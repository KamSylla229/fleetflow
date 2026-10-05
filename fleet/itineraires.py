"""Les trois axes routiers sur lesquels circule la flotte de démonstration.

Ce module ne connaît pas Django : il n'importe que la bibliothèque standard.
C'est délibéré — ce sont des données géographiques et un peu de trigonométrie,
rien qui dépende d'une base ou d'une requête. On peut donc le tester sans
lancer Django du tout, et le réutiliser ailleurs tel quel.

Les coordonnées sont **approximatives** : elles suivent le tracé général des
routes nationales, pas leur géométrie exacte. Elles suffisent pour une
simulation de démonstration ; le jour où un vrai boîtier remontera des points,
ce module ne servira plus qu'à la démo.
"""

import math
from dataclasses import dataclass

# Rayon moyen de la Terre, en kilomètres. La formule de haversine suppose une
# sphère : l'erreur est de l'ordre de 0,5 %, sans importance ici.
RAYON_TERRE_KM = 6371.0

# Dimensions du dessin de la page Carte. Ce sont les seules coordonnées de
# sortie du module : le gabarit pose un viewBox de cette taille, et le
# navigateur l'étire à la place disponible. Travailler dans un repère fixe
# évite au JavaScript de connaître la largeur du conteneur.
VUE_LARGEUR = 1000
VUE_HAUTEUR = 700

# Marge libre autour du tracé, en part de chaque côté du dessin. Sans elle, les
# villes des extrémités — Cotonou en bas, Parakou en haut — auraient leur nom
# coupé par le bord.
MARGE_VUE = 0.08

# De combien le trait de côte descend sous la ville la plus méridionale.
# 0,06° font environ sept kilomètres : assez pour que Cotonou et Ouidah soient
# visiblement sur la terre et non dans l'eau, assez peu pour que le golfe
# reste au bord du dessin.
MARGE_COTE_DEGRES = 0.06


def distance_km(depart, arrivee):
    """Distance à vol d'oiseau entre deux points (latitude, longitude).

    Formule de haversine. On ne peut pas se contenter de Pythagore sur les
    degrés : un degré de longitude vaut 111 km à l'équateur et 0 au pôle. Au
    Bénin (6° à 12° de latitude) l'écart serait déjà de plusieurs pour cent.
    """
    lat1, lon1 = depart
    lat2, lon2 = arrivee

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * RAYON_TERRE_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Itineraire:
    """Une polyligne d'étapes nommées, parcourue par une progression de 0 à 1.

    `etapes` est une suite de triplets (nom, latitude, longitude) : la première
    est le départ, la dernière l'arrivée. `point_a(t)` interpole le long de
    cette polyligne — t = 0 au départ, t = 1 à l'arrivée, t = 0,5 à mi-distance
    **réelle** et non à mi-chemin dans la liste des étapes.

    Les noms étaient jusqu'ici en commentaire à côté de chaque couple de
    coordonnées. La page Carte doit les afficher : un commentaire ne se lit pas
    depuis un gabarit. Les mettre dans la donnée n'invente rien, et évite la
    seule autre solution — une liste de noms en parallèle — qui aurait pu se
    désynchroniser des coordonnées sans que rien ne le signale.

    `points` reste disponible, et reste une suite de couples : tout le code de
    simulation et de distance s'en sert, et n'a pas à connaître les noms.

    frozen=True : un itinéraire est une donnée de référence, personne ne doit
    pouvoir le modifier en cours de route.
    """

    code: str
    libelle: str
    etapes: tuple
    # Rapport entre la distance réellement parcourue sur la route et la
    # longueur de la polyligne. Une polyligne qui relie des villes en ligne
    # droite sous-estime toujours la route : entre Cotonou et Parakou elle
    # mesure 357 km alors que le compteur d'un camion en affiche environ 410.
    # L'écart, ce sont les virages entre les villes. Plutôt que d'inventer de
    # faux points pour rallonger artificiellement le tracé — ce qui aurait
    # placé les camions à côté de la route — on garde une géographie honnête
    # et on multiplie la *distance* par ce coefficient.
    sinuosite: float = 1.0

    @property
    def points(self):
        """Les coordonnées seules, dans l'ordre du parcours.

        Reconstruite à chaque appel, comme `longueurs_cumulees` : la liste fait
        huit étapes au plus, et un cache sur un objet figé demanderait des
        contorsions pour aucun gain mesurable.
        """
        return tuple(
            (latitude, longitude) for _nom, latitude, longitude in self.etapes
        )

    @property
    def villes(self):
        """Les noms des étapes, dans l'ordre du parcours."""
        return tuple(nom for nom, _latitude, _longitude in self.etapes)

    @property
    def longueurs_cumulees(self):
        """Distance depuis le départ à chaque sommet de la polyligne.

        Calculée à chaque appel plutôt que mise en cache : la liste fait sept
        points, le calcul est instantané, et un cache sur un objet figé
        demanderait des contorsions pour aucun gain mesurable.
        """
        cumul = [0.0]
        for precedent, suivant in zip(self.points, self.points[1:]):
            cumul.append(cumul[-1] + distance_km(precedent, suivant))
        return cumul

    @property
    def longueur_polyligne_km(self):
        """Longueur du tracé à vol d'oiseau, de sommet en sommet."""
        return self.longueurs_cumulees[-1]

    @property
    def longueur_km(self):
        """Distance routière réelle : c'est elle qui sert à la cinématique.

        Un camion à 60 km/h parcourt 60 kilomètres de *route* en une heure, pas
        60 kilomètres à vol d'oiseau. C'est donc cette longueur qui convertit
        une distance parcourue en progression le long de l'itinéraire.
        """
        return self.longueur_polyligne_km * self.sinuosite

    def point_a(self, t):
        """Coordonnées (latitude, longitude) à la progression t.

        t est borné à [0, 1] : une progression hors de l'itinéraire n'a pas de
        sens, et renvoyer le départ ou l'arrivée est plus utile que lever une
        exception au milieu d'une boucle de simulation.
        """
        if t <= 0:
            return self.points[0]
        if t >= 1:
            return self.points[-1]

        # On place le point sur la polyligne, pas sur la distance routière :
        # la sinuosité est supposée uniforme, donc une progression de 50 % de
        # la route correspond à 50 % du tracé.
        cumul = self.longueurs_cumulees
        cible = t * cumul[-1]

        # On cherche le segment qui contient la distance visée, puis on
        # interpole linéairement entre ses deux extrémités. Sur quelques
        # dizaines de kilomètres, interpoler les degrés en ligne droite est
        # suffisamment juste.
        for index in range(1, len(cumul)):
            if cible <= cumul[index]:
                longueur_segment = cumul[index] - cumul[index - 1]
                if longueur_segment == 0:
                    return self.points[index]
                part = (cible - cumul[index - 1]) / longueur_segment
                (lat1, lon1) = self.points[index - 1]
                (lat2, lon2) = self.points[index]
                return (lat1 + (lat2 - lat1) * part, lon1 + (lon2 - lon1) * part)

        return self.points[-1]


# --- Les trois itinéraires de la démonstration ------------------------------
# Coordonnées approximatives des villes traversées, du sud vers le nord.

COTONOU_PARAKOU = Itineraire(
    code="cotonou_parakou",
    libelle="Cotonou → Parakou (RNIE 2)",
    etapes=(
        ("Cotonou", 6.3703, 2.3912),
        ("Abomey-Calavi", 6.4486, 2.3556),
        ("Allada", 6.6658, 2.1511),
        ("Bohicon", 7.1781, 2.0667),
        ("Dassa-Zoumè", 7.7500, 2.1833),
        ("Savè", 8.0344, 2.4864),
        ("Tchaourou", 8.8869, 2.5964),
        ("Parakou", 9.3372, 2.6303),
    ),
    # 365 km de tracé pour environ 410 km de route.
    sinuosite=1.12,
)

COTONOU_PORTO_NOVO = Itineraire(
    code="cotonou_porto_novo",
    libelle="Cotonou → Porto-Novo",
    etapes=(
        ("Cotonou", 6.3703, 2.3912),
        ("Sèmè-Kpodji", 6.3833, 2.6167),
        ("Porto-Novo", 6.4969, 2.6283),
    ),
    # Trajet court et direct : la route s'écarte peu de la ligne droite.
    sinuosite=1.06,
)

COTONOU_LOKOSSA = Itineraire(
    code="cotonou_lokossa",
    libelle="Cotonou → Lokossa",
    etapes=(
        ("Cotonou", 6.3703, 2.3912),
        ("Ouidah", 6.3667, 2.0833),
        ("Comè", 6.4000, 1.8833),
        ("Lokossa", 6.6389, 1.7167),
    ),
    # 89 km de tracé pour environ 100 km de route côtière puis intérieure.
    sinuosite=1.13,
)

ITINERAIRES = {
    itineraire.code: itineraire
    for itineraire in (COTONOU_PARAKOU, COTONOU_PORTO_NOVO, COTONOU_LOKOSSA)
}

# Prêt à être passé à un CharField(choices=...). Le modèle importe ce module,
# jamais l'inverse : les données géographiques n'ont pas à connaître la base.
CHOIX_ITINERAIRE = [
    (itineraire.code, itineraire.libelle) for itineraire in ITINERAIRES.values()
]


def itineraire_par_code(code):
    """L'itinéraire portant ce code, ou None si le code est vide ou inconnu.

    None plutôt qu'une exception : un véhicule sans itinéraire est un cas
    normal (il n'a pas encore été affecté à un axe), pas une erreur.
    """
    return ITINERAIRES.get(code or "")


# --- Projection vers le dessin de la page Carte ------------------------------
#
# La page Carte ne dessine pas une carte au sens géographique : elle dessine un
# schéma lisible des trois axes, dans un repère fixe de VUE_LARGEUR sur
# VUE_HAUTEUR. Trois fonctions suffisent, et elles vivent ici parce que ce sont
# des mathématiques sur des coordonnées — rien qui dépende de Django, donc rien
# qui demande une base de données pour être éprouvé.


def projeter_degres(latitude, longitude):
    """(x, y) en degrés, longitude corrigée par le cosinus de SA latitude.

    Un degré de longitude ne vaut pas la même distance partout : 111 km à
    l'équateur, zéro au pôle. Sans correction, le Bénin paraîtrait plus large
    en haut qu'en bas, et les trois axes ne se rejoindraient plus vraiment à
    Cotonou.

    Le cosinus est pris sur la latitude **de chaque point**, et non sur une
    latitude moyenne commune. C'est la différence entre cette projection
    (sinusoïdale) et un simple aplatissement : avec une constante, la
    correction serait absorbée par la mise à l'échelle qui suit et n'aurait
    aucun effet visible. Point par point, elle en a un.
    """
    return (longitude * math.cos(math.radians(latitude)), latitude)


def cadre_projection(itineraires=None):
    """Boîte englobante (x_min, x_max, y_min, y_max) de tous les points connus.

    « Tous les points » veut dire : toutes les étapes des trois itinéraires. Le
    cadrage ne dépend donc pas de la position des camions — il est le même à
    chaque rendu, et la carte ne se recadre jamais toute seule sous les yeux de
    l'utilisateur.
    """
    if itineraires is None:
        itineraires = ITINERAIRES.values()

    abscisses = []
    ordonnees = []
    for itineraire in itineraires:
        for latitude, longitude in itineraire.points:
            x, y = projeter_degres(latitude, longitude)
            abscisses.append(x)
            ordonnees.append(y)

    if not abscisses:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(abscisses), max(abscisses), min(ordonnees), max(ordonnees))


def projeter(latitude, longitude, cadre=None):
    """(x, y) dans le repère du dessin, marge comprise.

    Les coins de la boîte englobante tombent exactement sur la marge : le
    point le plus à l'ouest sur x = 80, le plus au nord sur y = 56, avec les
    valeurs par défaut.

    **y est inversé** : la latitude la plus grande donne le y le plus petit,
    parce que l'axe des ordonnées d'un SVG descend. Le nord est donc en haut,
    ce qui n'a l'air d'une évidence que tant qu'on ne l'a pas oublié.

    `cadre` est calculé si on ne le fournit pas. Un appelant qui projette
    beaucoup de points a intérêt à le calculer une fois et à le passer.
    """
    x_min, x_max, y_min, y_max = cadre if cadre is not None else cadre_projection()
    x, y = projeter_degres(latitude, longitude)

    marge_x = VUE_LARGEUR * MARGE_VUE
    marge_y = VUE_HAUTEUR * MARGE_VUE
    utile_x = VUE_LARGEUR - 2 * marge_x
    utile_y = VUE_HAUTEUR - 2 * marge_y

    # Une boîte plate — un seul point, ou tous alignés — ne doit pas faire
    # diviser par zéro. On centre alors sur cet axe.
    etendue_x = x_max - x_min
    etendue_y = y_max - y_min
    part_x = 0.5 if etendue_x == 0 else (x - x_min) / etendue_x
    part_y = 0.5 if etendue_y == 0 else (y - y_min) / etendue_y

    return (
        round(marge_x + part_x * utile_x, 2),
        round(marge_y + (1 - part_y) * utile_y, 2),
    )


def latitude_cote(itineraires=None):
    """Latitude du trait de côte : juste au sud de la ville la plus méridionale.

    Le golfe est dessiné comme un simple rectangle sous cette latitude. Le
    définir à partir des données plutôt que de le placer à l'œil garantit
    qu'aucune ville, et aucun point d'itinéraire, ne se retrouve dans l'eau —
    ce qu'un test vérifie.
    """
    if itineraires is None:
        itineraires = ITINERAIRES.values()

    latitudes = [
        latitude
        for itineraire in itineraires
        for latitude, _longitude in itineraire.points
    ]
    if not latitudes:
        return 0.0
    return min(latitudes) - MARGE_COTE_DEGRES
