"""Commande de démonstration : remplit la base avec un jeu de données béninois.

Utilisation :
    python manage.py seed_demo

La commande est ré-exécutable : elle vide d'abord les tables de FleetFlow, puis
les recrée. On peut donc la relancer autant de fois qu'on veut sans créer de
doublons ni violer la contrainte d'unicité sur les immatriculations.

Toutes les dates sont calculées par rapport à la date du jour, afin que les
alertes d'échéance restent pertinentes quel que soit le jour de la démonstration.

Les données produites respectent les règles appliquées par fleet/services.py :
un seul véhicule engagé sur une mission en cours à la fois, des relevés de
compteur strictement croissants dans le temps pour chaque véhicule. Sans cela,
la démonstration afficherait des consommations négatives et contredirait les
refus que le service oppose à l'utilisateur.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from fleet.models import (
    Chauffeur,
    Document,
    Entretien,
    Mission,
    PleinCarburant,
    Vehicule,
)

# --- Données de référence ----------------------------------------------------
# Elles sont regroupées ici, en haut du fichier, pour qu'on puisse les relire
# et les ajuster sans toucher à la logique de création.

# (marque, modèle, type, année, kilométrage de départ)
VEHICULES = [
    ("Mercedes-Benz", "Actros 1840", Vehicule.TypeVehicule.CAMION, 2016, 412_000),
    ("Mitsubishi", "Canter FE85", Vehicule.TypeVehicule.CAMION, 2018, 268_000),
    ("Hyundai", "HD78", Vehicule.TypeVehicule.CAMION, 2019, 154_000),
    ("Toyota", "Hiace", Vehicule.TypeVehicule.UTILITAIRE, 2017, 321_000),
    ("Peugeot", "Partner", Vehicule.TypeVehicule.UTILITAIRE, 2020, 96_000),
    ("Ford", "Transit", Vehicule.TypeVehicule.UTILITAIRE, 2015, 385_000),
    ("Toyota", "Corolla", Vehicule.TypeVehicule.VOITURE, 2021, 62_000),
    ("Hyundai", "Accent", Vehicule.TypeVehicule.VOITURE, 2019, 118_000),
    ("Bajaj", "Boxer 100", Vehicule.TypeVehicule.MOTO, 2022, 24_000),
    ("Haojue", "DK150", Vehicule.TypeVehicule.MOTO, 2023, 11_500),
]

# Plaques au format béninois : deux lettres, quatre chiffres, puis RB
# (République du Bénin).
IMMATRICULATIONS = [
    "AB 1234 RB",
    "AC 4821 RB",
    "AD 7734 RB",
    "AE 2096 RB",
    "AF 5517 RB",
    "AG 8043 RB",
    "AH 3162 RB",
    "AJ 6708 RB",
    "AK 9255 RB",
    "AL 1487 RB",
]

# (nom, téléphone, numéro de permis)
CHAUFFEURS = [
    ("Rodrigue Hounkpatin", "+229 01 97 45 12 33", "BJ-2018-004512"),
    ("Aïssatou Zinsou", "+229 01 96 22 87 04", "BJ-2019-011238"),
    ("Sylvain Dossou", "+229 01 95 61 40 77", "BJ-2017-008901"),
    ("Mireille Tchibozo", "+229 01 66 18 93 25", "BJ-2021-023476"),
    ("Ibrahim Yessoufou", "+229 01 94 77 05 61", "BJ-2016-002187"),
    ("Gildas Ahouandjinou", "+229 01 91 30 66 12", "BJ-2022-031055"),
]

# (départ, destination, distance approximative en km)
TRAJETS = [
    ("Cotonou", "Parakou", 410),
    ("Cotonou", "Lomé", 150),
    ("Cotonou", "Porto-Novo", 35),
    ("Cotonou", "Bohicon", 130),
]

PRESTATAIRES = [
    "Garage Sodji — Akpakpa",
    "CFAO Motors Bénin",
    "Atelier mécanique Zogbo",
    "Garage Tokpa Auto",
    "Pneus Service Cotonou",
]

ASSUREURS = ["NSIA Assurances", "Sunu Assurances", "Africaine des Assurances"]

# Prix observés à la pompe, en FCFA par litre.
PRIX_LITRE_FCFA = [Decimal("690"), Decimal("700"), Decimal("715"), Decimal("750")]

# Fourchettes de coût par type d'entretien, en FCFA.
COUTS_ENTRETIEN = {
    Entretien.TypeEntretien.VIDANGE: (25_000, 45_000),
    Entretien.TypeEntretien.PNEUS: (120_000, 260_000),
    Entretien.TypeEntretien.FREINS: (60_000, 95_000),
    Entretien.TypeEntretien.AUTRE: (15_000, 80_000),
}

# Par type de véhicule : (consommation de référence en L/100 km, kilomètres
# parcourus entre deux pleins). Ces deux nombres servent à calculer un volume
# de carburant cohérent avec la distance : sans cela, la consommation affichée
# par l'application serait fantaisiste.
CARBURANT_REFERENCE = {
    Vehicule.TypeVehicule.CAMION: (Decimal("32"), 800),
    Vehicule.TypeVehicule.UTILITAIRE: (Decimal("11"), 500),
    Vehicule.TypeVehicule.VOITURE: (Decimal("8"), 450),
    Vehicule.TypeVehicule.MOTO: (Decimal("3"), 200),
}

# Identifiants du compte de démonstration, créés uniquement quand DEBUG=True.
DEMO_UTILISATEUR = "demo"
DEMO_MOT_DE_PASSE = "demo1234"


class Command(BaseCommand):
    help = (
        "Remplit la base avec un jeu de données de démonstration béninois. "
        "ATTENTION : supprime au préalable toutes les données FleetFlow "
        "existantes. Refuse de tourner si DEBUG=False."
    )

    # @transaction.atomic : tout ce que fait la méthode est enregistré en une
    # seule transaction. Si une erreur survient au milieu, la base revient à son
    # état initial — on ne se retrouve jamais avec un jeu de données à moitié
    # créé.
    @transaction.atomic
    def handle(self, *args, **options):
        # Garde-fou : cette commande détruit toutes les données FleetFlow, pas
        # seulement celles qu'elle a créées. DEBUG=False signifie « ce n'est
        # pas un poste de développement » : on refuse d'aller plus loin plutôt
        # que de risquer d'effacer les données d'un client.
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo est réservé au développement : cette commande "
                "supprime toutes les données FleetFlow. Elle refuse de tourner "
                "avec DEBUG=False."
            )

        # random.seed fixe la suite de nombres « aléatoires » : deux exécutions
        # produisent exactement les mêmes données. C'est plus confortable pour
        # une démonstration, et cela rend un éventuel bug reproductible.
        random.seed(20260928)

        self.aujourdhui = timezone.localdate()

        self._vider_donnees()
        vehicules = self._creer_vehicules()
        chauffeurs = self._creer_chauffeurs()
        documents = self._creer_documents(vehicules)
        missions = self._creer_missions(vehicules, chauffeurs)
        pleins = self._creer_pleins(vehicules, chauffeurs)
        entretiens = self._creer_entretiens(vehicules)
        utilisateur = self._creer_utilisateur_demo()

        self._afficher_resume(
            vehicules, chauffeurs, documents, missions, pleins, entretiens, utilisateur
        )

    # --- Nettoyage -----------------------------------------------------------

    def _vider_donnees(self):
        """Supprime les données existantes, enfants avant parents.

        L'ordre est imposé par on_delete=PROTECT : tant qu'une mission, un
        entretien, un plein ou un document référence un véhicule, Django refuse
        de supprimer ce véhicule. On efface donc d'abord tout ce qui pointe vers
        Vehicule et Chauffeur, puis seulement ensuite ces deux tables.
        """
        Mission.objects.all().delete()
        PleinCarburant.objects.all().delete()
        Entretien.objects.all().delete()
        Document.objects.all().delete()
        Vehicule.objects.all().delete()
        Chauffeur.objects.all().delete()
        self.stdout.write("Données de démonstration précédentes supprimées.")

    # --- Création ------------------------------------------------------------

    def _creer_vehicules(self):
        """Crée les 10 véhicules.

        Le statut n'est pas tiré au hasard : il doit rester cohérent avec les
        missions créées plus bas, puisque services.creer_mission() refuse
        d'affecter un véhicule déjà engagé ou en maintenance. Seul le véhicule
        d'indice 0 portera la mission en cours ; les indices 3 et 7 sont mis en
        maintenance pour que la démonstration ait de quoi montrer un refus.
        """
        statuts = {
            0: Vehicule.Statut.EN_MISSION,
            3: Vehicule.Statut.EN_MAINTENANCE,
            7: Vehicule.Statut.EN_MAINTENANCE,
        }
        vehicules = []

        for index, (marque, modele, type_vehicule, annee, km) in enumerate(VEHICULES):
            vehicules.append(
                Vehicule.objects.create(
                    immatriculation=IMMATRICULATIONS[index],
                    marque=marque,
                    modele=modele,
                    annee=annee,
                    type_vehicule=type_vehicule,
                    kilometrage=km,
                    statut=statuts.get(index, Vehicule.Statut.DISPONIBLE),
                    actif=True,
                )
            )

        return vehicules

    def _creer_chauffeurs(self):
        """Crée les 6 chauffeurs, dont un permis qui expire sous 30 jours."""
        chauffeurs = []

        for index, (nom, telephone, permis) in enumerate(CHAUFFEURS):
            # Cas de démonstration imposé : le premier chauffeur doit renouveler
            # son permis dans moins de 30 jours.
            if index == 0:
                expiration = self.aujourdhui + timedelta(days=19)
            else:
                expiration = self.aujourdhui + timedelta(
                    days=random.randint(120, 1200)
                )

            chauffeurs.append(
                Chauffeur.objects.create(
                    nom=nom,
                    telephone=telephone,
                    numero_permis=permis,
                    date_expiration_permis=expiration,
                    actif=True,
                )
            )

        return chauffeurs

    def _creer_documents(self, vehicules):
        """Crée les pièces administratives de chaque véhicule.

        Depuis le Jour 2, les échéances ne sont plus des champs de Vehicule : ce
        sont des lignes de la table Document. Les cas d'alerte attendus par la
        démonstration sont donc créés ici.
        """
        documents = []

        for index, vehicule in enumerate(vehicules):
            # Cas imposés, calculés depuis la date du jour :
            #   - véhicules 0 et 1 : assurance qui expire dans moins de 15 jours
            #   - véhicule 2       : visite technique déjà dépassée
            if index == 0:
                fin_assurance = self.aujourdhui + timedelta(days=6)
            elif index == 1:
                fin_assurance = self.aujourdhui + timedelta(days=13)
            else:
                fin_assurance = self.aujourdhui + timedelta(
                    days=random.randint(45, 400)
                )

            if index == 2:
                visite_technique = self.aujourdhui - timedelta(days=24)
            else:
                visite_technique = self.aujourdhui + timedelta(
                    days=random.randint(30, 330)
                )

            documents.append(
                Document.objects.create(
                    vehicule=vehicule,
                    type_document=Document.TypeDocument.ASSURANCE,
                    numero=f"POL-{2025 + index % 2}-{10_000 + index * 137}",
                    # Une police d'assurance court sur un an : la date
                    # d'émission se déduit de l'échéance.
                    date_emission=fin_assurance - timedelta(days=365),
                    date_expiration=fin_assurance,
                    commentaire=random.choice(ASSUREURS),
                )
            )

            documents.append(
                Document.objects.create(
                    vehicule=vehicule,
                    type_document=Document.TypeDocument.VISITE_TECHNIQUE,
                    numero=f"VT-{40_000 + index * 311}",
                    date_emission=visite_technique - timedelta(days=365),
                    date_expiration=visite_technique,
                )
            )

            # La carte grise ne porte pas d'échéance : date_expiration reste
            # NULL, et l'application l'affichera avec un badge neutre.
            documents.append(
                Document.objects.create(
                    vehicule=vehicule,
                    type_document=Document.TypeDocument.CARTE_GRISE,
                    numero=f"CG-{vehicule.immatriculation.replace(' ', '')}",
                    date_emission=None,
                    date_expiration=None,
                    commentaire="Pièce sans échéance.",
                )
            )

            # Seuls les camions ont besoin d'une licence de transport de
            # marchandises.
            if vehicule.type_vehicule == Vehicule.TypeVehicule.CAMION:
                documents.append(
                    Document.objects.create(
                        vehicule=vehicule,
                        type_document=Document.TypeDocument.LICENCE_TRANSPORT,
                        numero=f"LT-BJ-{7_000 + index * 53}",
                        date_expiration=self.aujourdhui
                        + timedelta(days=random.randint(60, 500)),
                    )
                )

        return documents

    def _creer_missions(self, vehicules, chauffeurs):
        """Crée 25 missions étalées de deux mois en arrière à deux semaines devant.

        Une seule mission se retrouve au statut EN_COURS, celle dont la date de
        départ tombe aujourd'hui. C'est volontaire : la règle d'engagement du
        Jour 2 veut qu'un véhicule et un chauffeur ne puissent porter qu'une
        mission en cours à la fois. Des données de démonstration qui violeraient
        cette règle rendraient le refus du service incompréhensible.
        """
        missions = []

        # On suit le compteur kilométrique de chaque véhicule au fil des
        # missions, pour que les relevés restent croissants et cohérents.
        compteurs = {v.pk: v.kilometrage for v in vehicules}

        for index in range(25):
            vehicule = vehicules[index % len(vehicules)]
            chauffeur = chauffeurs[index % len(chauffeurs)]
            depart, destination, distance = TRAJETS[index % len(TRAJETS)]

            # Les missions s'échelonnent tous les 3 jours, de J-60 à J+12.
            date_depart = self.aujourdhui + timedelta(days=-60 + index * 3)

            km_depart = compteurs[vehicule.pk]
            date_arrivee = None
            km_arrivee = None
            commentaire = ""

            if date_depart > self.aujourdhui:
                statut = Mission.Statut.PLANIFIEE
            elif date_depart == self.aujourdhui:
                statut = Mission.Statut.EN_COURS
                commentaire = "Départ effectué ce matin, retour attendu demain."
            elif index % 8 == 3:
                statut = Mission.Statut.ANNULEE
                commentaire = "Annulée : client injoignable au chargement."
            else:
                statut = Mission.Statut.TERMINEE
                # Le kilométrage réel dépasse un peu la distance théorique
                # (détours, circulation dans Cotonou).
                km_parcourus = distance + random.randint(5, 40)
                km_arrivee = km_depart + km_parcourus
                date_arrivee = date_depart + timedelta(
                    days=1 if distance > 200 else 0
                )
                compteurs[vehicule.pk] = km_arrivee

            missions.append(
                Mission.objects.create(
                    vehicule=vehicule,
                    chauffeur=chauffeur,
                    depart=depart,
                    destination=destination,
                    date_depart=date_depart,
                    date_arrivee=date_arrivee,
                    km_depart=km_depart,
                    km_arrivee=km_arrivee,
                    statut=statut,
                    commentaire=commentaire,
                )
            )

        # Les compteurs des véhicules sont mis à jour avec les kilomètres
        # réellement parcourus pendant les missions terminées.
        for vehicule in vehicules:
            vehicule.kilometrage = compteurs[vehicule.pk]
            vehicule.save(update_fields=["kilometrage"])

        return missions

    def _creer_pleins(self, vehicules, chauffeurs):
        """Crée deux pleins par véhicule, cohérents dans le temps.

        Deux pleins et non un seul, parce que la consommation se calcule entre
        deux passages à la pompe : avec un seul relevé, l'application ne peut
        rien afficher. Les dates et les compteurs progressent ensemble, et le
        volume est déduit de la distance parcourue, pour que la consommation
        affichée ressemble à celle d'un vrai véhicule.

        Le Jour 1 tirait ces compteurs au hasard autour du kilométrage courant :
        rien ne garantissait qu'un plein plus ancien porte un relevé plus
        faible, ce qui produisait des consommations négatives.
        """
        pleins = []

        for index, vehicule in enumerate(vehicules):
            consommation, ecart_km = CARBURANT_REFERENCE[vehicule.type_vehicule]

            # Le plein le plus récent est relevé un peu avant le kilométrage
            # actuel ; le précédent, un « plein » plus tôt.
            km_recent = vehicule.kilometrage - 150
            km_precedent = km_recent - ecart_km

            for rang, (km_compteur, jours) in enumerate(
                [(km_precedent, 45), (km_recent, 12)]
            ):
                # Une variation de quelques pour cent autour de la consommation
                # de référence : conduite, charge et état de la route.
                variation = Decimal(random.choice(["0.92", "1.00", "1.09"]))
                litres = (
                    consommation * Decimal(ecart_km) / Decimal(100) * variation
                ).quantize(Decimal("0.01"))

                pleins.append(
                    PleinCarburant.objects.create(
                        vehicule=vehicule,
                        chauffeur=chauffeurs[(index + rang) % len(chauffeurs)],
                        date=self.aujourdhui - timedelta(days=jours),
                        litres=litres,
                        prix_litre=random.choice(PRIX_LITRE_FCFA),
                        km_compteur=km_compteur,
                    )
                )

        return pleins

    def _creer_entretiens(self, vehicules):
        """Crée 8 entretiens répartis sur les quatre derniers mois."""
        entretiens = []
        types = list(Entretien.TypeEntretien)

        for index in range(8):
            vehicule = vehicules[index % len(vehicules)]
            type_entretien = types[index % len(types)]
            cout_min, cout_max = COUTS_ENTRETIEN[type_entretien]

            km_intervention = vehicule.kilometrage - random.randint(1_000, 15_000)

            entretiens.append(
                Entretien.objects.create(
                    vehicule=vehicule,
                    type_entretien=type_entretien,
                    date=self.aujourdhui - timedelta(days=15 + index * 13),
                    km=km_intervention,
                    cout=Decimal(random.randint(cout_min, cout_max)),
                    prestataire=random.choice(PRESTATAIRES),
                    # Une vidange se planifie tous les 5 000 km ; les autres
                    # interventions n'ont pas d'échéance kilométrique fixe.
                    prochaine_echeance_km=(
                        km_intervention + 5_000
                        if type_entretien == Entretien.TypeEntretien.VIDANGE
                        else None
                    ),
                )
            )

        return entretiens

    def _creer_utilisateur_demo(self):
        """Crée (ou réinitialise) le compte de démonstration.

        Sans compte, impossible de se connecter : toutes les vues de FleetFlow
        exigent une authentification. Le mot de passe est en clair dans le
        code, ce qui est acceptable pour un compte de démonstration créé
        uniquement quand DEBUG=True — et c'est pour cela que le garde-fou de
        handle() est indispensable.
        """
        Utilisateur = get_user_model()

        utilisateur, cree = Utilisateur.objects.get_or_create(
            username=DEMO_UTILISATEUR,
            defaults={
                "email": "demo@fleetflow.test",
                "first_name": "Compte",
                "last_name": "Démonstration",
                # is_staff donne aussi accès à /admin/, pratique pour comparer
                # l'interface de FleetFlow et celle de l'admin Django.
                "is_staff": True,
                "is_superuser": True,
            },
        )
        # set_password hache le mot de passe : on n'écrit jamais un mot de passe
        # en clair dans la colonne password.
        utilisateur.set_password(DEMO_MOT_DE_PASSE)
        utilisateur.save()

        self.stdout.write(
            "Compte de démonstration "
            + ("créé" if cree else "mot de passe réinitialisé")
            + f" : {DEMO_UTILISATEUR} / {DEMO_MOT_DE_PASSE}"
        )
        return utilisateur

    # --- Compte rendu --------------------------------------------------------

    def _afficher_resume(
        self, vehicules, chauffeurs, documents, missions, pleins, entretiens, utilisateur
    ):
        """Affiche ce qui a été créé, puis les alertes attendues pour la démo."""
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(vehicules)} véhicules, {len(chauffeurs)} chauffeurs, "
                f"{len(documents)} documents, {len(missions)} missions, "
                f"{len(pleins)} pleins et {len(entretiens)} entretiens créés."
            )
        )

        self.stdout.write("")
        self.stdout.write("Cas de démonstration (date du jour : "
                          f"{self.aujourdhui:%d/%m/%Y}) :")

        for vehicule in vehicules:
            jours = vehicule.jours_avant_fin_assurance
            if jours is not None and jours < 15:
                self.stdout.write(
                    f"  - {vehicule.immatriculation} : assurance à échéance "
                    f"dans {jours} jours ({vehicule.date_fin_assurance:%d/%m/%Y})"
                )
            visite = vehicule.date_visite_technique
            if visite is not None and visite < self.aujourdhui:
                retard = (self.aujourdhui - visite).days
                self.stdout.write(
                    f"  - {vehicule.immatriculation} : visite technique expirée "
                    f"depuis {retard} jours ({visite:%d/%m/%Y})"
                )

        for chauffeur in chauffeurs:
            jours = chauffeur.jours_avant_expiration_permis
            if jours < 30:
                self.stdout.write(
                    f"  - {chauffeur.nom} : permis à renouveler dans {jours} "
                    f"jours ({chauffeur.date_expiration_permis:%d/%m/%Y})"
                )

        mission_en_cours = next(
            (m for m in missions if m.statut == Mission.Statut.EN_COURS), None
        )
        if mission_en_cours is not None:
            self.stdout.write(
                f"  - {mission_en_cours.vehicule.immatriculation} est engagé sur "
                # Fleche ASCII et non le caractere U+2192 : la console
                # Windows ecrit en cp1252, qui ne sait pas le representer, et
                # le plantage annulerait toute la transaction du seed.
                f"la mission {mission_en_cours.depart} -> "
                f"{mission_en_cours.destination} : toute nouvelle affectation "
                "de ce véhicule sera refusée."
            )

        self.stdout.write("")
        self.stdout.write(
            f"Connexion : {DEMO_UTILISATEUR} / {DEMO_MOT_DE_PASSE}"
        )
