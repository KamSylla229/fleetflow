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
    """Une polyligne de points, parcourue par une progression entre 0 et 1.

    `points` est une suite de couples (latitude, longitude) : le premier est le
    départ, le dernier l'arrivée. `point_a(t)` interpole le long de cette
    polyligne — t = 0 au départ, t = 1 à l'arrivée, t = 0,5 à mi-distance
    **réelle** et non à mi-chemin dans la liste des points.

    frozen=True : un itinéraire est une donnée de référence, personne ne doit
    pouvoir le modifier en cours de route.
    """

    code: str
    libelle: str
    points: tuple
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
    points=(
        (6.3703, 2.3912),   # Cotonou
        (6.4486, 2.3556),   # Abomey-Calavi
        (6.6658, 2.1511),   # Allada
        (7.1781, 2.0667),   # Bohicon
        (7.7500, 2.1833),   # Dassa-Zoumè
        (8.0344, 2.4864),   # Savè
        (8.8869, 2.5964),   # Tchaourou
        (9.3372, 2.6303),   # Parakou
    ),
    # 365 km de tracé pour environ 410 km de route.
    sinuosite=1.12,
)

COTONOU_PORTO_NOVO = Itineraire(
    code="cotonou_porto_novo",
    libelle="Cotonou → Porto-Novo",
    points=(
        (6.3703, 2.3912),   # Cotonou
        (6.3833, 2.6167),   # Sèmè-Kpodji
        (6.4969, 2.6283),   # Porto-Novo
    ),
    # Trajet court et direct : la route s'écarte peu de la ligne droite.
    sinuosite=1.06,
)

COTONOU_LOKOSSA = Itineraire(
    code="cotonou_lokossa",
    libelle="Cotonou → Lokossa",
    points=(
        (6.3703, 2.3912),   # Cotonou
        (6.3667, 2.0833),   # Ouidah
        (6.4000, 1.8833),   # Comè
        (6.6389, 1.7167),   # Lokossa
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
