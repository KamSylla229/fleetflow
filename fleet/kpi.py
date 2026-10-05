"""Les cinq indicateurs du tableau de bord, calculés en un appel.

Ce module n'invente aucun calcul : il assemble ce que `fleet/services.py` et
`fleet/echeances.py` savent déjà faire. C'est volontaire — si le tableau de
bord recalculait les kilomètres ou la consommation à sa façon, il finirait par
afficher d'autres chiffres que le rapport quotidien, et personne ne saurait
lequel croire.

Le budget de requêtes est la contrainte principale : le cahier des charges
demande une page en moins d'une seconde, et la page est rafraîchie toutes les
dix secondes. Tout est donc en un nombre **constant** de requêtes, qui ne
croît pas avec la taille de la flotte.
"""

from django.utils import timezone

from fleet import services
from fleet.echeances import classer_echeances, collecter_echeances
from fleet.models import Vehicule


def calculer_kpi(today=None):
    """Renvoie les indicateurs du tableau de bord sous forme de dictionnaire.

    `today` est la date de référence, dans le fuseau du projet
    (Africa/Porto-Novo). Elle est passée explicitement à
    `kilometres_parcourus` pour que le tableau de bord et le rapport quotidien
    comptent exactement les mêmes kilomètres — un relevé de 23 h 30 locale
    appartient au jour local, pas au jour UTC suivant.

    Au-delà des cinq indicateurs demandés, le dictionnaire porte ce que la
    page affiche autour : le détail des kilomètres par camion, le classement
    des échéances et les alertes ouvertes. Ils ne coûtent aucune requête de
    plus, puisqu'ils viennent des mêmes appels.
    """
    today = today or timezone.localdate()

    # Une seule requête pour la flotte active, avec le dernier relevé GPS de
    # chaque camion en sous-requêtes corrélées. statut_gps() lira ces
    # annotations sans toucher la base.
    # prefetch_related("documents") sert à collecter_echeances plus bas : en
    # lui passant cette liste déjà chargée, on évite qu'il recharge la même
    # flotte pour son propre compte. C'est une requête de moins sur une page
    # rafraîchie toutes les dix secondes.
    vehicules = list(
        services.annoter_dernieres_positions(
            Vehicule.objects.filter(actif=True)
            .select_related("fournisseur_gps")
            .prefetch_related("documents")
        )
    )
    identifiants = [vehicule.pk for vehicule in vehicules]

    maintenant = timezone.now()
    statuts = {
        vehicule.pk: services.statut_gps(vehicule, maintenant=maintenant)
        for vehicule in vehicules
    }

    # « En service » reprend la définition d'indicateurs_flotte : actif et
    # disponible ou en mission. Elle exclut donc aussi les camions hors
    # service, ce qui est plus juste que « pas en maintenance » au sens
    # littéral — un camion hors service n'est pas en service.
    en_service = [
        vehicule
        for vehicule in vehicules
        if vehicule.statut
        in (Vehicule.Statut.DISPONIBLE, Vehicule.Statut.EN_MISSION)
    ]
    en_maintenance = [
        vehicule
        for vehicule in vehicules
        if vehicule.statut == Vehicule.Statut.EN_MAINTENANCE
    ]
    en_mouvement = [
        vehicule
        for vehicule in vehicules
        if statuts[vehicule.pk].code == services.GPS_EN_ROUTE
    ]

    kilometres = services.kilometres_parcourus(today)
    consommation = services.consommation_moyenne_ponderee(identifiants)
    classement = classer_echeances(
        collecter_echeances(vehicules=vehicules), today=today
    )
    alertes = list(services.alertes_ouvertes())

    echeances_60j = (
        len(classement["depassees"])
        + len(classement["sous_30_j"])
        + len(classement["sous_60_j"])
    )

    return {
        "today": today,
        # --- Les cinq indicateurs ------------------------------------------
        "en_service": len(en_service),
        "en_mouvement": len(en_mouvement),
        "km_du_jour": round(sum(km for _vehicule, km in kilometres), 1),
        "consommation_moy": consommation,
        "echeances_60j": echeances_60j,
        # --- Ce que la page affiche autour ---------------------------------
        "total_camions": len(vehicules),
        "en_maintenance": len(en_maintenance),
        "depassees": len(classement["depassees"]),
        "km_par_camion": kilometres,
        "classement": classement,
        "alertes": alertes,
        "statuts": statuts,
    }
