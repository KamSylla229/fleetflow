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

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .models import Chauffeur, Entretien, Mission, PleinCarburant, Vehicule

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
    classe_css: str
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
    classe_css: str
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
            classe_css="bg-secondary",
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
            classe_css="bg-danger",
            jours=jours,
        )
    if jours <= seuil_jours:
        return StatutEcheance(
            code=ECHEANCE_BIENTOT,
            libelle=f"Expire dans {jours} j",
            classe_css="bg-warning text-dark",
            jours=jours,
        )
    return StatutEcheance(
        code=ECHEANCE_VALIDE,
        libelle=f"Valide ({jours} j)",
        classe_css="bg-success",
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
            classe_css="bg-dark",
            jours=None,
        )
    return statut_echeance(vehicule.date_fin_assurance)


def statut_visite_technique(vehicule):
    """Statut de la visite technique, avec la même distinction."""
    if vehicule.document_visite_technique is None:
        return StatutEcheance(
            code=ECHEANCE_ABSENTE,
            libelle="Aucun procès-verbal",
            classe_css="bg-dark",
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
            classe_css="bg-secondary",
            km_restants=None,
        )

    km_restants = entretien.prochaine_echeance_km - kilometrage_actuel

    if km_restants < 0:
        return StatutKilometrique(
            code=ECHEANCE_EXPIREE,
            libelle=f"En retard de {abs(km_restants)} km",
            classe_css="bg-danger",
            km_restants=km_restants,
        )
    if km_restants <= SEUIL_ALERTE_ENTRETIEN_KM:
        return StatutKilometrique(
            code=ECHEANCE_BIENTOT,
            libelle=f"À faire dans {km_restants} km",
            classe_css="bg-warning text-dark",
            km_restants=km_restants,
        )
    return StatutKilometrique(
        code=ECHEANCE_VALIDE,
        libelle=f"À venir dans {km_restants} km",
        classe_css="bg-success",
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
