"""Logique métier de FleetFlow : tout ce qui décide, vérifie et refuse.

Pourquoi ce fichier existe
--------------------------
Une vue Django a un seul travail : lire une requête HTTP, appeler le métier,
choisir un gabarit. Dès qu'une règle de gestion s'écrit dans une vue, elle
devient inaccessible au reste du projet : la commande de seed, l'admin, un
import de fichier ou un futur travail planifié devraient la réécrire, et les
deux copies divergeraient. Ici, la règle est écrite une fois et appelée de
partout — y compris par les tests, qui n'ont alors besoin ni de client HTTP ni
de session.

Conventions de ce module
------------------------
- Tout refus lève une ``ValidationError`` portant un message en français,
  directement affichable à l'utilisateur. Les vues n'ont qu'à l'attraper.
- Les fonctions qui écrivent sont ``@transaction.atomic`` : soit tout est
  enregistré, soit rien.
- Les fonctions qui touchent au compteur ou au statut d'un véhicule le
  rechargent avec ``select_for_update()``, pour qu'un deuxième gestionnaire ne
  puisse pas travailler sur la même ligne en même temps.
- Les arguments sont nommés (le ``*`` dans les signatures). ``depart`` et
  ``destination`` sont deux chaînes de caractères, ``vehicule`` et
  ``chauffeur`` deux objets : un appel positionnel finirait tôt ou tard par
  les inverser sans que rien ne proteste.

Une limite à connaître sur ``select_for_update()``
--------------------------------------------------
Le verrou est posé par la base de données, pas par Django. PostgreSQL et MySQL
l'appliquent réellement : la deuxième transaction attend que la première ait
fini. SQLite, utilisé ici en développement, ne connaît pas
``SELECT … FOR UPDATE`` et ignore silencieusement la demande — il verrouille
de toute façon le fichier entier en écriture. Le code est donc écrit pour le
jour où la base passera sur PostgreSQL, et la protection n'est pas illusoire
en attendant : SQLite sérialise les écritures.
"""

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from .models import (
    Chauffeur,
    Entretien,
    FournisseurGPS,
    Mission,
    PleinCarburant,
    PositionGPS,
    Vehicule,
)

# --- Réglages métier ---------------------------------------------------------
# Les seuils sont des constantes nommées, pas des nombres écrits au milieu du
# code : on sait où les changer, et leur nom explique ce qu'ils veulent dire.

# Un document ou un permis est signalé « bientôt expiré » ce nombre de jours
# avant son échéance.
SEUIL_ALERTE_ECHEANCE_JOURS = 30

# Un entretien est signalé « proche » ce nombre de kilomètres avant l'échéance.
SEUIL_ALERTE_ENTRETIEN_KM = 500

# Intervalle usuel entre deux interventions du même type, en kilomètres.
# None signifie « pas d'échéance kilométrique » : on ne prévoit pas un
# remplacement de plaquettes de frein au kilomètre près.
INTERVALLE_ENTRETIEN_KM = {
    Entretien.TypeEntretien.VIDANGE: 5_000,
    Entretien.TypeEntretien.FREINS: 30_000,
    Entretien.TypeEntretien.PNEUS: 40_000,
    Entretien.TypeEntretien.AUTRE: None,
}

# Codes des statuts d'échéance. Des constantes plutôt que des chaînes écrites
# à la main dans les gabarits : une faute de frappe dans un template ne lève
# aucune erreur, elle affiche simplement le mauvais badge sans rien signaler.
ECHEANCE_VALIDE = "valide"
ECHEANCE_BIENTOT = "bientot"
ECHEANCE_EXPIREE = "expiree"
ECHEANCE_SANS = "sans_echeance"
# « Aucune pièce enregistrée » n'est ni « valide » ni « sans échéance » :
# un véhicule dont le dossier d'assurance est vide doit alerter, pas
# rassurer. C'est le pendant, côté affichage, du None renvoyé par
# Vehicule.assurance_expiree.
ECHEANCE_ABSENTE = "absente"


@dataclass(frozen=True)
class StatutEcheance:
    """Le verdict porté sur une date d'échéance, prêt à afficher.

    ``frozen=True`` rend l'objet non modifiable : un gabarit ne peut pas
    changer par accident le libellé ou la couleur d'un statut déjà calculé.
    On renvoie un objet et non un simple code, parce que l'appelant a besoin
    des quatre informations ensemble — le code pour comparer, le libellé pour
    écrire, la classe CSS pour colorer, le nombre de jours pour détailler.
    """

    code: str
    libelle: str
    # Le ton est semantique (« vert », « ambre », « rouge », « neutre »), pas
    # une classe CSS. Les services ne connaissent pas la feuille de style :
    # c'est partials/badge.html qui traduit un ton en classe. Le jour ou le
    # theme change, services.py n'est pas touche.
    ton: str
    jours: int | None

    @property
    def est_alerte(self):
        """True si l'échéance mérite l'attention du gestionnaire."""
        return self.code in (ECHEANCE_BIENTOT, ECHEANCE_EXPIREE, ECHEANCE_ABSENTE)


@dataclass(frozen=True)
class StatutKilometrique:
    """Le même principe, pour une échéance exprimée en kilomètres."""

    code: str
    libelle: str
    ton: str
    km_restants: int | None

    @property
    def est_alerte(self):
        return self.code in (ECHEANCE_BIENTOT, ECHEANCE_EXPIREE)


# --- Échéances ---------------------------------------------------------------


def statut_echeance(date_expiration, seuil_jours=SEUIL_ALERTE_ECHEANCE_JOURS):
    """Qualifie une date d'échéance : valide, bientôt expirée, expirée.

    Sert aussi bien aux documents d'un véhicule qu'au permis d'un chauffeur :
    c'est la même question posée à une date, il n'y a donc aucune raison
    d'écrire la règle deux fois. La fonction prend une date et non un objet,
    ce qui la rend utilisable partout et triviale à tester.

    ``date_expiration`` à None signifie « pièce sans échéance » (une carte
    grise, par exemple) : ni verte ni rouge, un badge neutre.
    """
    if date_expiration is None:
        return StatutEcheance(
            code=ECHEANCE_SANS,
            libelle="Sans échéance",
            ton="neutre",
            jours=None,
        )

    # localdate() et non date.today() : la date du jour dans le fuseau du
    # projet (Africa/Porto-Novo), alors que Django travaille en UTC en
    # interne. Près de minuit, date.today() se tromperait d'un jour — et un
    # jour d'écart suffit à laisser rouler un véhicule non assuré.
    jours = (date_expiration - timezone.localdate()).days

    if jours < 0:
        return StatutEcheance(
            code=ECHEANCE_EXPIREE,
            libelle=f"Expiré depuis {abs(jours)} j",
            ton="rouge",
            jours=jours,
        )
    if jours <= seuil_jours:
        return StatutEcheance(
            code=ECHEANCE_BIENTOT,
            libelle=f"Expire dans {jours} j",
            ton="ambre",
            jours=jours,
        )
    return StatutEcheance(
        code=ECHEANCE_VALIDE,
        libelle=f"Valide ({jours} j)",
        ton="vert",
        jours=jours,
    )


def documents_avec_statut(vehicule):
    """Les documents d'un véhicule, chacun enrichi de son statut d'échéance.

    On attache le résultat sur l'instance (``document.statut = …``). C'est un
    attribut Python ordinaire, posé le temps de la requête HTTP : rien n'est
    écrit en base, et aucun champ n'a besoin d'exister pour cela. Le calcul se
    fait ici plutôt que dans le gabarit, parce qu'un gabarit Django ne sait pas
    appeler une fonction avec des arguments — et parce qu'une règle métier n'a
    rien à faire dans du HTML.
    """
    documents = list(vehicule.documents.all())
    for document in documents:
        document.statut = statut_echeance(document.date_expiration)
    return documents


def statut_assurance(vehicule):
    """Statut de l'assurance d'un véhicule, absence d'attestation comprise.

    statut_echeance(None) répond « sans échéance », ce qui convient à une carte
    grise mais pas à une assurance : une police d'assurance a toujours une fin,
    et ne pas en trouver signifie que le dossier est incomplet. On distingue
    donc les deux cas au lieu d'afficher un badge neutre rassurant.
    """
    if vehicule.document_assurance is None:
        return StatutEcheance(
            code=ECHEANCE_ABSENTE,
            libelle="Aucune attestation",
            ton="ambre",
            jours=None,
        )
    return statut_echeance(vehicule.date_fin_assurance)


def statut_visite_technique(vehicule):
    """Statut de la visite technique, avec la même distinction."""
    if vehicule.document_visite_technique is None:
        return StatutEcheance(
            code=ECHEANCE_ABSENTE,
            libelle="Aucun procès-verbal",
            ton="ambre",
            jours=None,
        )
    return statut_echeance(vehicule.date_visite_technique)


def statut_permis(chauffeur):
    """Le statut du permis d'un chauffeur, avec la même règle que les documents."""
    return statut_echeance(chauffeur.date_expiration_permis)


def statut_prochain_entretien(entretien, kilometrage_actuel):
    """Situe la prochaine échéance d'entretien par rapport au compteur actuel.

    Une échéance d'entretien ne se compte pas en jours mais en kilomètres :
    une vidange se fait tous les 5 000 km, que le camion les parcoure en un
    mois ou en six. C'est pourquoi cette fonction ne réutilise pas
    statut_echeance() — même forme de réponse, unité différente.
    """
    if entretien.prochaine_echeance_km is None:
        return StatutKilometrique(
            code=ECHEANCE_SANS,
            libelle="Sans échéance",
            ton="neutre",
            km_restants=None,
        )

    km_restants = entretien.prochaine_echeance_km - kilometrage_actuel

    if km_restants < 0:
        return StatutKilometrique(
            code=ECHEANCE_EXPIREE,
            libelle=f"En retard de {abs(km_restants)} km",
            ton="rouge",
            km_restants=km_restants,
        )
    if km_restants <= SEUIL_ALERTE_ENTRETIEN_KM:
        return StatutKilometrique(
            code=ECHEANCE_BIENTOT,
            libelle=f"À faire dans {km_restants} km",
            ton="ambre",
            km_restants=km_restants,
        )
    return StatutKilometrique(
        code=ECHEANCE_VALIDE,
        libelle=f"À venir dans {km_restants} km",
        ton="vert",
        km_restants=km_restants,
    )


# --- Missions ----------------------------------------------------------------


def missions_en_cours(*, vehicule=None, chauffeur=None):
    """Les missions actuellement en cours, filtrées sur un véhicule ou un chauffeur.

    C'est ici, et nulle part ailleurs, qu'est écrit ce que « être engagé »
    veut dire : porter une mission au statut EN_COURS. Les missions PLANIFIEE
    ne bloquent pas, car une mission prévue dans trois semaines n'empêche pas
    de rouler aujourd'hui — gérer ces chevauchements demande un vrai planning,
    inscrit au BACKLOG.
    """
    queryset = Mission.objects.filter(statut=Mission.Statut.EN_COURS)
    if vehicule is not None:
        queryset = queryset.filter(vehicule=vehicule)
    if chauffeur is not None:
        queryset = queryset.filter(chauffeur=chauffeur)
    return queryset


@transaction.atomic
def creer_mission(
    *,
    vehicule,
    chauffeur,
    depart,
    destination,
    date_depart,
    km_depart=None,
    commentaire="",
):
    """Affecte un véhicule et un chauffeur à une nouvelle mission, en cours.

    Une mission créée démarre immédiatement : elle engage le véhicule et le
    chauffeur, qui ne peuvent plus en prendre d'autre avant sa clôture. C'est
    la raison du refus d'une date de départ future — enregistrer une mission
    « en cours » pour la semaine prochaine bloquerait un camion sans qu'il
    roule.

    Lève une ValidationError, sans rien écrire, si l'affectation est refusée.
    """
    aujourdhui = timezone.localdate()

    if date_depart > aujourdhui:
        raise ValidationError(
            "La date de départ ne peut pas être dans le futur : une mission "
            "créée démarre immédiatement. La planification des missions à "
            "venir n'est pas encore gérée."
        )

    # On recharge les deux objets depuis la base avec un verrou, au lieu de
    # faire confiance aux instances reçues. Deux raisons : elles peuvent être
    # périmées (lues il y a trente secondes, avant l'affectation d'un
    # collègue), et le verrou empêche une deuxième transaction de vérifier les
    # mêmes conditions en parallèle pour affecter le même camion. Sans lui,
    # deux gestionnaires cliquant en même temps passeraient tous les deux les
    # contrôles, et le véhicule se retrouverait sur deux missions.
    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)
    chauffeur = Chauffeur.objects.select_for_update().get(pk=chauffeur.pk)

    if not vehicule.actif:
        raise ValidationError(
            f"Le véhicule {vehicule.immatriculation} ne fait plus partie de la "
            "flotte active : il ne peut pas recevoir de mission."
        )
    if not chauffeur.actif:
        raise ValidationError(
            f"{chauffeur.nom} n'est plus un chauffeur actif : il ne peut pas "
            "recevoir de mission."
        )
    if chauffeur.permis_expire:
        raise ValidationError(
            f"Le permis de {chauffeur.nom} a expiré le "
            f"{chauffeur.date_expiration_permis:%d/%m/%Y} : l'affectation est "
            "refusée."
        )
    if vehicule.statut == Vehicule.Statut.EN_MAINTENANCE:
        raise ValidationError(
            f"Le véhicule {vehicule.immatriculation} est en maintenance : il "
            "doit en sortir avant de repartir en mission."
        )
    if vehicule.statut == Vehicule.Statut.HORS_SERVICE:
        raise ValidationError(
            f"Le véhicule {vehicule.immatriculation} est hors service : il ne "
            "peut pas recevoir de mission."
        )

    mission_du_vehicule = missions_en_cours(vehicule=vehicule).first()
    if mission_du_vehicule is not None:
        raise ValidationError(
            f"Le véhicule {vehicule.immatriculation} est déjà engagé sur la "
            f"mission {mission_du_vehicule.depart} - "
            f"{mission_du_vehicule.destination}, partie le "
            f"{mission_du_vehicule.date_depart:%d/%m/%Y}. Clôturez-la avant "
            "de l'affecter à nouveau."
        )

    mission_du_chauffeur = missions_en_cours(chauffeur=chauffeur).first()
    if mission_du_chauffeur is not None:
        raise ValidationError(
            f"{chauffeur.nom} est déjà en mission vers "
            f"{mission_du_chauffeur.destination} depuis le "
            f"{mission_du_chauffeur.date_depart:%d/%m/%Y}. Clôturez cette "
            "mission avant de lui en confier une autre."
        )

    if km_depart is None:
        # Par défaut, le relevé de départ est le compteur connu du véhicule :
        # c'est l'information la plus fiable dont on dispose, et cela évite
        # une saisie manuelle de plus.
        km_depart = vehicule.kilometrage
    elif km_depart < vehicule.kilometrage:
        raise ValidationError(
            f"Le kilométrage de départ saisi ({km_depart} km) est inférieur au "
            f"compteur enregistré du véhicule ({vehicule.kilometrage} km). "
            "Un compteur ne revient pas en arrière : vérifiez le relevé."
        )

    mission = Mission.objects.create(
        vehicule=vehicule,
        chauffeur=chauffeur,
        depart=depart,
        destination=destination,
        date_depart=date_depart,
        km_depart=km_depart,
        statut=Mission.Statut.EN_COURS,
        commentaire=commentaire,
    )

    # Le statut du véhicule est une donnée dénormalisée : il répète une
    # information déduisible des missions. On l'entretient ici, dans la même
    # transaction que la création, pour que les deux ne puissent pas se
    # contredire. C'est le prix d'un affichage immédiat dans les listes, sans
    # requête supplémentaire par ligne.
    vehicule.statut = Vehicule.Statut.EN_MISSION
    vehicule.save(update_fields=["statut"])

    return mission


@transaction.atomic
def cloturer_mission(mission, *, date_arrivee, km_arrivee, commentaire=None):
    """Clôture une mission en cours et reporte les kilomètres sur le véhicule.

    C'est le seul endroit du projet qui fait avancer ``Vehicule.kilometrage``
    au retour d'une mission. Le compteur du véhicule et les relevés des
    missions restent ainsi cohérents.
    """
    aujourdhui = timezone.localdate()

    # On relit la mission sous verrou : si deux personnes clôturent la même
    # mission en même temps, la seconde verra le statut déjà passé à TERMINEE
    # et sera refusée, au lieu d'ajouter une seconde fois les kilomètres au
    # compteur du véhicule.
    mission = (
        Mission.objects.select_for_update().select_related("vehicule").get(pk=mission.pk)
    )

    if mission.statut != Mission.Statut.EN_COURS:
        raise ValidationError(
            "Seule une mission en cours peut être clôturée ; celle-ci est au "
            f"statut « {mission.get_statut_display()} »."
        )
    if km_arrivee <= mission.km_depart:
        raise ValidationError(
            f"Le kilométrage d'arrivée ({km_arrivee} km) doit être strictement "
            f"supérieur à celui du départ ({mission.km_depart} km)."
        )
    if date_arrivee < mission.date_depart:
        raise ValidationError(
            f"La date d'arrivée ({date_arrivee:%d/%m/%Y}) ne peut pas précéder "
            f"la date de départ ({mission.date_depart:%d/%m/%Y})."
        )
    if date_arrivee > aujourdhui:
        raise ValidationError(
            "La date d'arrivée ne peut pas être dans le futur : on ne clôture "
            "que des missions déjà revenues."
        )

    vehicule = Vehicule.objects.select_for_update().get(pk=mission.vehicule_id)

    mission.date_arrivee = date_arrivee
    mission.km_arrivee = km_arrivee
    mission.statut = Mission.Statut.TERMINEE
    champs_modifies = ["date_arrivee", "km_arrivee", "statut"]
    if commentaire is not None:
        mission.commentaire = commentaire
        champs_modifies.append("commentaire")
    # update_fields limite la requête UPDATE aux colonnes réellement
    # modifiées : on n'écrase pas au passage un champ qu'un autre traitement
    # aurait changé entre-temps.
    mission.save(update_fields=champs_modifies)

    # Le compteur ne peut que monter. Un plein de carburant enregistré pendant
    # la mission a pu le pousser plus loin que ce relevé d'arrivée ; on garde
    # alors la valeur la plus élevée plutôt que de faire reculer le compteur.
    if km_arrivee > vehicule.kilometrage:
        vehicule.kilometrage = km_arrivee
    vehicule.statut = Vehicule.Statut.DISPONIBLE
    vehicule.save(update_fields=["kilometrage", "statut"])

    return mission


# --- Flotte : entrée et sortie -----------------------------------------------


@transaction.atomic
def basculer_activation_vehicule(vehicule):
    """Sort un véhicule de la flotte active, ou l'y fait revenir.

    FleetFlow ne supprime pas : les clés étrangères sont en PROTECT et
    l'historique d'exploitation doit rester consultable — c'est lui qui prouve
    les coûts et le suivi d'entretien. « Supprimer » un véhicule, ici, veut
    donc dire passer ``actif`` à False : il disparaît des listes de travail,
    ses missions passées restent.

    Un véhicule engagé sur une mission en cours ne peut pas sortir de la
    flotte : sa mission n'aurait plus de véhicule affecté à un objet actif, et
    le chauffeur resterait bloqué sans pouvoir clôturer proprement.
    """
    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)

    if vehicule.actif:
        mission = missions_en_cours(vehicule=vehicule).first()
        if mission is not None:
            raise ValidationError(
                f"Le véhicule {vehicule.immatriculation} est en mission vers "
                f"{mission.destination}. Clôturez cette mission avant de le "
                "sortir de la flotte."
            )
        vehicule.actif = False
        # Un véhicule sorti de la flotte n'est pas « disponible » : laisser son
        # statut à DISPONIBLE le ferait apparaître comme affectable dans les
        # listes déroulantes.
        vehicule.statut = Vehicule.Statut.HORS_SERVICE
    else:
        vehicule.actif = True
        vehicule.statut = Vehicule.Statut.DISPONIBLE

    vehicule.save(update_fields=["actif", "statut"])
    return vehicule


@transaction.atomic
def basculer_activation_chauffeur(chauffeur):
    """Désactive un chauffeur, ou le réactive. Même principe que le véhicule."""
    chauffeur = Chauffeur.objects.select_for_update().get(pk=chauffeur.pk)

    if chauffeur.actif:
        mission = missions_en_cours(chauffeur=chauffeur).first()
        if mission is not None:
            raise ValidationError(
                f"{chauffeur.nom} est en mission vers {mission.destination}. "
                "Clôturez cette mission avant de le désactiver."
            )
        chauffeur.actif = False
    else:
        chauffeur.actif = True

    chauffeur.save(update_fields=["actif"])
    return chauffeur


# --- Carburant ---------------------------------------------------------------


def _consommation_entre(precedent, courant):
    """Consommation en L/100 km entre deux pleins successifs, ou None.

    Hypothèse assumée, dite « méthode du plein complet » : on suppose le
    réservoir rempli à ras bord à chaque passage. Le volume du plein courant
    correspond alors à ce qui a été brûlé depuis le précédent. Un demi-plein
    faussera le calcul ; c'est la méthode qu'emploient tous les carnets de
    bord papier, et elle reste la seule possible sans capteur embarqué.
    """
    if precedent is None:
        return None

    distance = courant.km_compteur - precedent.km_compteur
    if distance <= 0:
        # Données incohérentes (saisie par l'admin, import). On renvoie None
        # plutôt qu'un nombre absurde ou une division par zéro : « inconnu »
        # est une réponse honnête, « 0 L/100 km » serait un mensonge.
        return None

    litres = courant.litres / Decimal(distance) * Decimal(100)
    # quantize plutôt que round() : on reste en Decimal de bout en bout, sans
    # jamais repasser par un flottant qui réintroduirait des arrondis.
    return litres.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculer_consommation(plein):
    """Consommation d'un plein par rapport au précédent du même véhicule.

    Renvoie None pour le premier plein d'un véhicule : sans point de
    comparaison, la consommation n'est pas inconnue par accident, elle n'a
    simplement pas de sens.
    """
    precedent = (
        PleinCarburant.objects.filter(vehicule_id=plein.vehicule_id)
        # Deux pleins peuvent porter la même date ; la clé primaire, toujours
        # croissante, sert alors à les départager.
        .filter(
            models.Q(date__lt=plein.date)
            | models.Q(date=plein.date, pk__lt=plein.pk)
        )
        .order_by("-date", "-pk")
        .first()
    )
    return _consommation_entre(precedent, plein)


def annoter_consommations(pleins):
    """Attache sa consommation à chaque plein d'une liste, en une seule requête.

    Appeler calculer_consommation() dans une boucle sur vingt-cinq lignes
    déclencherait vingt-cinq requêtes — le problème « N+1 ». Ici, on charge en
    une fois l'historique des véhicules concernés, on reconstitue les paires
    en mémoire, puis on annote. Une requête, quel que soit le nombre de lignes
    affichées.
    """
    pleins = list(pleins)
    if not pleins:
        return pleins

    identifiants = {plein.vehicule_id for plein in pleins}
    historique = {}
    for plein in PleinCarburant.objects.filter(
        vehicule_id__in=identifiants
    ).order_by("vehicule_id", "date", "pk"):
        historique.setdefault(plein.vehicule_id, []).append(plein)

    # zip(liste, liste[1:]) associe chaque élément à son suivant : la façon la
    # plus lisible de parcourir une suite deux par deux.
    precedents = {}
    for pleins_du_vehicule in historique.values():
        for precedent, suivant in zip(pleins_du_vehicule, pleins_du_vehicule[1:]):
            precedents[suivant.pk] = precedent

    for plein in pleins:
        plein.consommation = _consommation_entre(precedents.get(plein.pk), plein)

    return pleins


@transaction.atomic
def enregistrer_plein(
    *, vehicule, chauffeur, date, litres, prix_litre, km_compteur, commentaire=None
):
    """Enregistre un passage à la pompe et renvoie (plein, consommation).

    Renvoie un couple et non le seul plein : la consommation est précisément
    ce que l'utilisateur veut voir après sa saisie, et elle vient d'être
    calculée ici. La lui faire redemander dans la vue serait une requête de
    plus pour une information déjà en main.
    """
    aujourdhui = timezone.localdate()

    if litres <= 0:
        raise ValidationError(
            "Le volume doit être strictement positif : un plein de zéro litre "
            "n'existe pas."
        )
    if prix_litre <= 0:
        raise ValidationError("Le prix du litre doit être strictement positif.")
    if date > aujourdhui:
        raise ValidationError("La date du plein ne peut pas être dans le futur.")

    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)

    # Le plein le plus récent connu pour ce véhicule sert de point de
    # comparaison. On refuse d'insérer une saisie antérieure : la consommation
    # se calcule de proche en proche, et un plein glissé au milieu de
    # l'historique fausserait celle du plein suivant, déjà affichée.
    precedent = vehicule.pleins.order_by("-date", "-pk").first()
    if precedent is not None:
        if date < precedent.date:
            raise ValidationError(
                f"Le dernier plein connu pour {vehicule.immatriculation} date "
                f"du {precedent.date:%d/%m/%Y}. Saisissez les pleins dans "
                "l'ordre chronologique."
            )
        if km_compteur <= precedent.km_compteur:
            raise ValidationError(
                f"Le compteur relevé ({km_compteur} km) doit dépasser celui du "
                f"plein précédent ({precedent.km_compteur} km, le "
                f"{precedent.date:%d/%m/%Y})."
            )

    plein = PleinCarburant.objects.create(
        vehicule=vehicule,
        chauffeur=chauffeur,
        date=date,
        litres=litres,
        prix_litre=prix_litre,
        km_compteur=km_compteur,
    )

    # Un relevé à la pompe est une observation directe du compteur : s'il
    # dépasse la valeur enregistrée, il la remplace.
    if km_compteur > vehicule.kilometrage:
        vehicule.kilometrage = km_compteur
        vehicule.save(update_fields=["kilometrage"])

    return plein, _consommation_entre(precedent, plein)


# --- Entretiens --------------------------------------------------------------


@transaction.atomic
def enregistrer_entretien(
    *,
    vehicule,
    type_entretien,
    date,
    km,
    cout,
    prestataire,
    prochaine_echeance_km=None,
):
    """Enregistre une intervention et calcule sa prochaine échéance.

    Si ``prochaine_echeance_km`` n'est pas fourni, il est déduit du type
    d'intervention via INTERVALLE_ENTRETIEN_KM. Le garagiste qui annonce une
    échéance différente peut toujours la saisir : la valeur explicite gagne
    sur la règle générale.
    """
    aujourdhui = timezone.localdate()

    if cout < 0:
        raise ValidationError("Le coût d'un entretien ne peut pas être négatif.")
    if date > aujourdhui:
        raise ValidationError(
            "La date de l'intervention ne peut pas être dans le futur : on "
            "enregistre un entretien réalisé, pas un entretien prévu."
        )

    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)

    dernier = vehicule.entretiens.order_by("-km").first()
    if dernier is not None and km < dernier.km:
        raise ValidationError(
            f"Un entretien est déjà enregistré à {dernier.km} km pour "
            f"{vehicule.immatriculation} : le relevé saisi ({km} km) lui est "
            "antérieur. Vérifiez le kilométrage."
        )

    if prochaine_echeance_km is None:
        intervalle = INTERVALLE_ENTRETIEN_KM.get(type_entretien)
        prochaine_echeance_km = km + intervalle if intervalle is not None else None
    elif prochaine_echeance_km <= km:
        raise ValidationError(
            f"La prochaine échéance ({prochaine_echeance_km} km) doit être "
            f"supérieure au kilométrage de l'intervention ({km} km)."
        )

    entretien = Entretien.objects.create(
        vehicule=vehicule,
        type_entretien=type_entretien,
        date=date,
        km=km,
        cout=cout,
        prestataire=prestataire,
        prochaine_echeance_km=prochaine_echeance_km,
    )

    if km > vehicule.kilometrage:
        vehicule.kilometrage = km
        vehicule.save(update_fields=["kilometrage"])

    return entretien


# --- Cumuls et annotations de liste -----------------------------------------


def _cumuls_carburant(identifiants):
    """Pour chaque véhicule, le total des litres et des kilomètres mesurables.

    « Mesurable » veut dire : entre deux pleins successifs. Le premier plein
    d'un véhicule ne compte pas, puisqu'aucune distance ne lui est rattachée.

    Renvoie {pk: (litres, kilometres)} et ne coûte **qu'une requête**, quel
    que soit le nombre de véhicules. C'est la brique commune à la
    consommation d'une ligne de tableau et à celle de toute la flotte : deux
    calculs séparés finiraient par ne plus donner le même chiffre.
    """
    if not identifiants:
        return {}

    historique = {}
    for plein in PleinCarburant.objects.filter(
        vehicule_id__in=identifiants
    ).order_by("vehicule_id", "date", "pk"):
        historique.setdefault(plein.vehicule_id, []).append(plein)

    cumuls = {}
    for identifiant, pleins_du_vehicule in historique.items():
        litres = Decimal("0")
        kilometres = 0
        for precedent, courant in zip(pleins_du_vehicule, pleins_du_vehicule[1:]):
            distance = courant.km_compteur - precedent.km_compteur
            if distance > 0:
                litres += courant.litres
                kilometres += distance
        cumuls[identifiant] = (litres, kilometres)
    return cumuls


def annoter_consommation_moyenne(vehicules):
    """Attache `consommation_moyenne` (L/100 km) à chaque véhicule d'une liste.

    None quand aucun couple de pleins n'est exploitable : un véhicule qui n'a
    qu'un seul plein n'a pas une consommation de zéro, il a une consommation
    inconnue.
    """
    vehicules = list(vehicules)
    cumuls = _cumuls_carburant([vehicule.pk for vehicule in vehicules])

    for vehicule in vehicules:
        litres, kilometres = cumuls.get(vehicule.pk, (Decimal("0"), 0))
        if kilometres > 0:
            vehicule.consommation_moyenne = (
                litres / Decimal(kilometres) * Decimal(100)
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        else:
            vehicule.consommation_moyenne = None
    return vehicules


def missions_en_cours_par_vehicule(vehicules):
    """{pk du véhicule: sa mission en cours}, en une seule requête.

    Sert à afficher le chauffeur affecté sur chaque ligne de la liste des
    camions. Appeler missions_en_cours() dans la boucle du gabarit coûterait
    une requête par ligne.
    """
    identifiants = [vehicule.pk for vehicule in vehicules]
    if not identifiants:
        return {}

    missions = (
        Mission.objects.filter(
            statut=Mission.Statut.EN_COURS, vehicule_id__in=identifiants
        )
        .select_related("chauffeur")
        .order_by("vehicule_id", "-date_depart")
    )
    # Un véhicule ne peut porter qu'une mission en cours (garanti par
    # creer_mission), mais on reste tolérant à des données importées à la main.
    return {mission.vehicule_id: mission for mission in missions}


# --- Indicateurs de flotte ---------------------------------------------------


@dataclass(frozen=True)
class IndicateursFlotte:
    """Les quatre chiffres affichés sous la liste des camions.

    Un objet plutôt qu'un dictionnaire : les champs sont nommés une fois ici,
    une faute de frappe dans un gabarit (`indicateurs.age_moyenne`) affiche du
    vide au lieu de lever une erreur — mais au moins le contrat est écrit et
    testable.
    """

    total: int
    en_service: int
    kilometrage_cumule: int
    consommation_moyenne: Decimal | None
    age_moyen: Decimal | None
    annee_plus_ancien: int | None
    immobilises: tuple


def indicateurs_flotte(vehicules=None):
    """Calcule les indicateurs de la flotte active.

    Le calcul est ici et non dans la vue pour la même raison que le reste :
    le tableau de bord du Jour 3 et un futur export Excel afficheront les
    mêmes chiffres, et deux calculs séparés finiraient par ne plus dire la
    même chose.

    Coût : deux requêtes, quel que soit le nombre de camions — une pour les
    véhicules, une pour l'ensemble de leurs pleins.
    """
    if vehicules is None:
        vehicules = Vehicule.objects.filter(actif=True)
    vehicules = list(vehicules)

    if not vehicules:
        return IndicateursFlotte(
            total=0,
            en_service=0,
            kilometrage_cumule=0,
            consommation_moyenne=None,
            age_moyen=None,
            annee_plus_ancien=None,
            immobilises=(),
        )

    kilometrage_cumule = sum(vehicule.kilometrage for vehicule in vehicules)

    en_service = sum(
        1
        for vehicule in vehicules
        if vehicule.actif
        and vehicule.statut in (Vehicule.Statut.DISPONIBLE, Vehicule.Statut.EN_MISSION)
    )

    immobilises = tuple(
        vehicule.immatriculation
        for vehicule in vehicules
        if vehicule.statut == Vehicule.Statut.EN_MAINTENANCE
    )

    # Consommation moyenne *pondérée par la distance*, et non moyenne des
    # consommations : un camion qui fait 400 km doit peser deux fois plus
    # qu'un utilitaire qui en fait 200. On additionne donc les litres et les
    # kilomètres de toute la flotte avant de diviser. Faire la moyenne des
    # L/100 km individuels donnerait un chiffre que rien ne vérifie.
    cumuls = _cumuls_carburant([vehicule.pk for vehicule in vehicules])
    total_litres = sum((litres for litres, _km in cumuls.values()), Decimal("0"))
    total_km = sum(km for _litres, km in cumuls.values())

    if total_km:
        consommation_moyenne = (
            total_litres / Decimal(total_km) * Decimal(100)
        ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    else:
        # Aucun couple de pleins exploitable : on ne sait pas, et on le dit.
        consommation_moyenne = None

    annees = [vehicule.annee for vehicule in vehicules if vehicule.annee]
    if annees:
        annee_courante = timezone.localdate().year
        age_moyen = (
            Decimal(sum(annee_courante - annee for annee in annees))
            / Decimal(len(annees))
        ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        annee_plus_ancien = min(annees)
    else:
        age_moyen = None
        annee_plus_ancien = None

    return IndicateursFlotte(
        total=len(vehicules),
        en_service=en_service,
        kilometrage_cumule=kilometrage_cumule,
        consommation_moyenne=consommation_moyenne,
        age_moyen=age_moyen,
        annee_plus_ancien=annee_plus_ancien,
        immobilises=immobilises,
    )


# --- Statuts affichables -----------------------------------------------------
# Traduire un statut en couleur est une décision métier, pas une décision de
# gabarit : « en maintenance » est-il inquiétant ou neutre ? La réponse est
# écrite ici, une fois, et les gabarits ne font que l'afficher.

TONS_STATUT_VEHICULE = {
    Vehicule.Statut.EN_MISSION: "vert",
    Vehicule.Statut.DISPONIBLE: "neutre",
    Vehicule.Statut.EN_MAINTENANCE: "ambre",
    Vehicule.Statut.HORS_SERVICE: "rouge",
}


@dataclass(frozen=True)
class StatutAffichable:
    """Un état prêt à poser dans une pastille : un ton et un libellé."""

    code: str
    libelle: str
    ton: str


def statut_operationnel(vehicule):
    """L'état d'exploitation d'un camion, prêt à afficher.

    À ne pas confondre avec le statut GPS (en route / à l'arrêt / sans
    signal), qui sera calculé à partir des positions remontées par le boîtier
    et n'existe pas encore. Celui-ci est saisi et vaut pour la flotte ;
    l'autre sera mesuré.
    """
    if not vehicule.actif:
        return StatutAffichable(code="sorti", libelle="Sorti de la flotte", ton="rouge")
    return StatutAffichable(
        code=vehicule.statut,
        libelle=vehicule.get_statut_display(),
        ton=TONS_STATUT_VEHICULE.get(vehicule.statut, "neutre"),
    )


# Sentinelle : « cet argument n'a pas été fourni ». On ne peut pas utiliser
# None pour ça, car None est une réponse légitime — « j'ai cherché, ce
# chauffeur n'est sur aucune mission ». Les deux cas demandent un comportement
# opposé : interroger la base, ou ne surtout pas l'interroger.
_NON_FOURNI = object()


def statut_chauffeur(chauffeur, mission_en_cours=_NON_FOURNI):
    """L'état d'un chauffeur : en mission, disponible ou désactivé.

    `mission_en_cours` peut être fourni par l'appelant quand il l'a déjà
    chargé — c'est ce qui évite une requête par ligne dans une liste. Passer
    explicitement None signifie « j'ai vérifié, il n'y en a pas » et ne
    déclenche aucune requête.
    """
    if not chauffeur.actif:
        return StatutAffichable(code="inactif", libelle="Désactivé", ton="rouge")

    if mission_en_cours is _NON_FOURNI:
        mission_en_cours = missions_en_cours(chauffeur=chauffeur).first()

    if mission_en_cours is not None:
        return StatutAffichable(code="en_mission", libelle="En mission", ton="vert")
    return StatutAffichable(code="disponible", libelle="Disponible", ton="neutre")


def missions_en_cours_par_chauffeur(chauffeurs):
    """{pk du chauffeur: sa mission en cours}, en une seule requête.

    Le pendant de missions_en_cours_par_vehicule, pour la liste des
    chauffeurs. Les deux fonctions restent distinctes plutôt qu'une seule
    paramétrée : deux lignes de plus, mais l'appelant lit ce qu'il obtient.
    """
    identifiants = [chauffeur.pk for chauffeur in chauffeurs]
    if not identifiants:
        return {}

    missions = (
        Mission.objects.filter(
            statut=Mission.Statut.EN_COURS, chauffeur_id__in=identifiants
        )
        .select_related("vehicule")
        .order_by("chauffeur_id", "-date_depart")
    )
    return {mission.chauffeur_id: mission for mission in missions}


TONS_STATUT_MISSION = {
    Mission.Statut.EN_COURS: "vert",
    Mission.Statut.PLANIFIEE: "ambre",
    Mission.Statut.TERMINEE: "neutre",
    # Une annulation n'est pas un incident technique, mais c'est une course
    # perdue : elle doit se voir dans une liste.
    Mission.Statut.ANNULEE: "rouge",
}


def statut_mission(mission):
    """L'état d'une mission, prêt à poser dans une pastille."""
    return StatutAffichable(
        code=mission.statut,
        libelle=mission.get_statut_display(),
        ton=TONS_STATUT_MISSION.get(mission.statut, "neutre"),
    )


# --- Suivi GPS ---------------------------------------------------------------
# Deux notions à ne pas confondre :
#   - le statut *opérationnel* (disponible, en mission, en maintenance) est
#     saisi par le gestionnaire et vaut pour la flotte ;
#   - le statut *GPS* (en route, à l'arrêt, sans signal) est **calculé** à
#     partir du dernier relevé du boîtier. Personne ne le saisit, et il peut
#     contredire le premier — c'est précisément à ça qu'il sert.

GPS_EN_ROUTE = "en_route"
GPS_A_ARRET = "a_arret"
GPS_SANS_SIGNAL = "sans_signal"
GPS_AUCUNE_DONNEE = "aucune_donnee"

TONS_STATUT_GPS = {
    GPS_EN_ROUTE: "vert",
    GPS_A_ARRET: "ambre",
    GPS_SANS_SIGNAL: "rouge",
    GPS_AUCUNE_DONNEE: "neutre",
}

LIBELLES_STATUT_GPS = {
    GPS_EN_ROUTE: "En route",
    GPS_A_ARRET: "À l'arrêt",
    GPS_SANS_SIGNAL: "Sans signal",
    GPS_AUCUNE_DONNEE: "Aucune donnée",
}

# Une vitesse tirée à ±15 % de la nominale, et une chance sur vingt d'être
# arrêté à un relevé donné : feu rouge, pesage, pause. Une simulation trop
# régulière se repère au premier coup d'œil et ne met à l'épreuve aucun des
# cas que l'application devra traiter.
VARIATION_VITESSE = 0.15
PROBABILITE_ARRET = 0.05


def seuil_sans_signal():
    """Durée au-delà de laquelle un boîtier est considéré muet.

    Lue dans les réglages à chaque appel et non figée à l'import : la
    démonstration abaisse le seuil à une minute dans le `.env` pour qu'on
    n'attende pas une demi-heure devant l'écran.
    """
    return timedelta(minutes=settings.FLEETFLOW_SEUIL_SANS_SIGNAL_MIN)


def derniere_position(vehicule):
    """Le relevé le plus récent de ce camion, ou None s'il n'y en a aucun.

    `Meta.ordering` de PositionGPS trie déjà du plus récent au plus ancien,
    et l'index composé (vehicule, -horodatage) rend cette requête immédiate
    même sur une table de plusieurs centaines de milliers de lignes.
    """
    return vehicule.positions.first()


def annoter_dernieres_positions(queryset):
    """Ajoute le dernier relevé de chaque camion, en une seule requête.

    Quatre sous-requêtes corrélées (une par colonne utile) exécutées dans la
    même requête que la liste. L'alternative — appeler derniere_position()
    dans la boucle du gabarit — coûterait une requête par ligne.

    OuterRef("pk") désigne le camion de la ligne en cours côté requête
    extérieure : c'est ce qui rend la sous-requête corrélée. Le `[:1]` est
    obligatoire, une sous-requête de colonne devant renvoyer une seule ligne.
    """
    derniere = PositionGPS.objects.filter(vehicule=OuterRef("pk")).order_by(
        "-horodatage", "-pk"
    )
    return queryset.annotate(
        gps_horodatage=Subquery(derniere.values("horodatage")[:1]),
        gps_vitesse_kmh=Subquery(derniere.values("vitesse_kmh")[:1]),
        gps_latitude=Subquery(derniere.values("latitude")[:1]),
        gps_longitude=Subquery(derniere.values("longitude")[:1]),
    )


def statut_gps(vehicule, maintenant=None):
    """Qualifie l'état GPS d'un camion d'après son dernier relevé.

    Quatre réponses possibles :
      - aucune_donnee : jamais rien reçu (pas de boîtier, ou boîtier neuf) ;
      - sans_signal   : le dernier relevé est plus vieux que le seuil ;
      - en_route      : relevé récent, vitesse non nulle ;
      - a_arret       : relevé récent, vitesse nulle.

    « Jamais rien reçu » et « plus rien depuis trois heures » sont distingués
    à dessein : le premier est un camion non équipé, le second une panne.

    Si le camion a été annoté par annoter_dernieres_positions(), les valeurs
    annotées sont utilisées et aucune requête n'est faite.
    """
    maintenant = maintenant or timezone.now()

    horodatage = getattr(vehicule, "gps_horodatage", _NON_FOURNI)
    if horodatage is _NON_FOURNI:
        position = derniere_position(vehicule)
        horodatage = position.horodatage if position is not None else None
        vitesse = position.vitesse_kmh if position is not None else None
    else:
        vitesse = getattr(vehicule, "gps_vitesse_kmh", None)

    if horodatage is None:
        code = GPS_AUCUNE_DONNEE
    elif maintenant - horodatage > seuil_sans_signal():
        # Comparaison stricte : un relevé pile à l'âge du seuil est encore
        # considéré comme reçu. La borne doit être décidée, pas subie.
        code = GPS_SANS_SIGNAL
    elif vitesse is not None and vitesse > 0:
        code = GPS_EN_ROUTE
    else:
        code = GPS_A_ARRET

    return StatutAffichable(
        code=code,
        libelle=LIBELLES_STATUT_GPS[code],
        ton=TONS_STATUT_GPS[code],
    )


@transaction.atomic
def basculer_signal(vehicule, *, coupe):
    """Coupe ou rétablit le signal d'un boîtier (interrupteur de démonstration).

    Passe par un service et non par une écriture directe pour la même raison
    que tout le reste : la commande `simuler_positions --couper` et un futur
    bouton dans l'interface doivent faire exactement la même chose.
    """
    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)
    vehicule.signal_coupe = coupe
    vehicule.save(update_fields=["signal_coupe"])
    return vehicule


@transaction.atomic
def avancer_position(vehicule, dt_secondes, rng=None, maintenant=None):
    """Fait avancer un camion sur son itinéraire et enregistre sa position.

    **Point d'entrée unique de la simulation.** Renvoie la PositionGPS créée,
    ou None si le camion n'avait pas à bouger — et dans ce cas rien n'est
    écrit du tout.

    Trois conditions pour qu'un relevé soit produit :
      - le camion a un itinéraire ;
      - il est engagé sur une mission en cours (un camion au dépôt ne remonte
        pas de trajet) ;
      - son signal n'est pas coupé.

    `rng` permet d'injecter un générateur aléatoire — `random.Random(42)` dans
    les tests. Sans cela, un test sur la distance parcourue réussirait ou
    échouerait selon le tirage, ce qui est la définition d'un test inutile.
    """
    if dt_secondes <= 0:
        raise ValidationError(
            "La durée écoulée doit être strictement positive pour faire "
            "avancer un camion."
        )

    rng = rng or random
    maintenant = maintenant or timezone.now()

    # Rechargement sous verrou : deux ticks qui se chevauchent — une boucle
    # lancée deux fois par distraction — ne doivent pas faire avancer le même
    # camion en parallèle depuis la même progression.
    vehicule = Vehicule.objects.select_for_update().get(pk=vehicule.pk)

    trace = vehicule.trace
    if trace is None or vehicule.signal_coupe:
        return None
    if not missions_en_cours(vehicule=vehicule).exists():
        return None

    if rng.random() < PROBABILITE_ARRET:
        vitesse_kmh = 0.0
        distance_km = 0.0
    else:
        facteur = 1 + rng.uniform(-VARIATION_VITESSE, VARIATION_VITESSE)
        vitesse_kmh = vehicule.vitesse_nominale_kmh * facteur
        distance_km = vitesse_kmh * dt_secondes / 3600

    # La progression avance de la fraction de l'itinéraire parcourue. C'est la
    # longueur *routière* qui sert ici : un camion à 60 km/h couvre 60 km de
    # route en une heure, pas 60 km à vol d'oiseau.
    progression = vehicule.progression + vehicule.direction * (
        distance_km / trace.longueur_km
    )
    direction = vehicule.direction

    # Aller-retour : arrivé au bout, le camion repart dans l'autre sens. Les
    # bornes sont posées à 1 et 0 plutôt que laissées déborder, sinon un tick
    # un peu long enverrait la progression à 1,04 et le marqueur hors du tracé.
    if progression >= 1:
        progression = 1.0
        direction = -1
    elif progression <= 0:
        progression = 0.0
        direction = 1

    latitude, longitude = trace.point_a(progression)

    position = PositionGPS.objects.create(
        vehicule=vehicule,
        latitude=Decimal(str(round(latitude, 6))),
        longitude=Decimal(str(round(longitude, 6))),
        vitesse_kmh=Decimal(str(round(vitesse_kmh, 1))),
        horodatage=maintenant,
        source=PositionGPS.Source.SIMULATION,
    )

    vehicule.progression = progression
    vehicule.direction = direction
    vehicule.save(update_fields=["progression", "direction"])

    if vehicule.fournisseur_gps_id:
        # .update() plutôt que de charger l'objet : une seule requête, et pas
        # de risque d'écraser au passage un autre champ du fournisseur qu'un
        # tick voisin viendrait de modifier.
        FournisseurGPS.objects.filter(pk=vehicule.fournisseur_gps_id).update(
            dernier_echange=position.horodatage
        )

    return position


def avancer_flotte(dt_secondes, rng=None, maintenant=None):
    """Un tick de simulation pour toute la flotte active.

    Renvoie la liste des couples (camion, position ou None). La boucle vit ici
    et non dans la commande : un futur travail planifié, ou un bouton
    « avancer » dans l'interface, doivent avancer la flotte exactement de la
    même façon.
    """
    resultats = []
    for vehicule in Vehicule.objects.filter(actif=True).select_related(
        "fournisseur_gps"
    ):
        resultats.append(
            (vehicule, avancer_position(vehicule, dt_secondes, rng=rng, maintenant=maintenant))
        )
    return resultats
