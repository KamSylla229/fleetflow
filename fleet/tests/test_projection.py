"""Tests de la projection géographique vers le dessin de la page Carte.

Tous héritent de `SimpleTestCase` : `fleet/itineraires.py` n'importe que la
bibliothèque standard, il n'y a donc **aucune base de données à créer**. C'est
le même choix que pour `classer_echeances` à la phase C, et pour la même
raison : les règles qu'on a le plus besoin d'éprouver sont celles qui tiennent
dans une fonction pure, et on les éprouve d'autant mieux qu'elles tournent en
quelques millisecondes.

Ce que ces tests protègent, c'est le cadrage : un dessin dont les bords
dépassent, un nord qui se retrouve en bas, ou une ville posée dans la mer
n'échouent nulle part ailleurs. Ils s'affichent, simplement faux.
"""

import math

from django.test import SimpleTestCase

from fleet.itineraires import (
    ITINERAIRES,
    MARGE_VUE,
    VUE_HAUTEUR,
    VUE_LARGEUR,
    Itineraire,
    cadre_projection,
    latitude_cote,
    projeter,
    projeter_degres,
)

MARGE_X = VUE_LARGEUR * MARGE_VUE
MARGE_Y = VUE_HAUTEUR * MARGE_VUE


def tous_les_points():
    """Les points des trois itinéraires, dédoublonnés."""
    return {
        point for itineraire in ITINERAIRES.values() for point in itineraire.points
    }


class CadreTest(SimpleTestCase):
    """La boîte englobante couvre toutes les étapes, et rien d'autre."""

    def test_le_cadre_englobe_tous_les_points(self):
        x_min, x_max, y_min, y_max = cadre_projection()
        for latitude, longitude in tous_les_points():
            x, y = projeter_degres(latitude, longitude)
            with self.subTest(point=(latitude, longitude)):
                self.assertGreaterEqual(x, x_min)
                self.assertLessEqual(x, x_max)
                self.assertGreaterEqual(y, y_min)
                self.assertLessEqual(y, y_max)

    def test_le_cadre_ne_depend_pas_des_camions(self):
        """Il est calculé sur les itinéraires, donc constant d'un rendu à l'autre.

        C'est ce qui garantit que la carte ne se recadre jamais toute seule :
        un camion qui avance ne déplace pas le dessin sous les yeux de
        l'utilisateur.
        """
        self.assertEqual(cadre_projection(), cadre_projection())

    def test_un_cadre_vide_ne_leve_rien(self):
        self.assertEqual(cadre_projection([]), (0.0, 0.0, 0.0, 0.0))


class ProjectionTest(SimpleTestCase):
    """Les quatre coins, le centre, et le sens du nord."""

    def setUp(self):
        self.cadre = cadre_projection()
        self.x_min, self.x_max, self.y_min, self.y_max = self.cadre

    def latitude_de(self, y):
        """L'ordonnée projetée EST la latitude : projeter_degres renvoie y = lat."""
        return y

    def longitude_de(self, x, latitude):
        """Inverse de la correction cosinus, pour fabriquer un point visé."""
        return x / math.cos(math.radians(latitude))

    def test_les_quatre_coins_de_la_boite_tombent_sur_la_marge(self):
        """Sud-ouest, sud-est, nord-ouest, nord-est, dans cet ordre.

        Les coins sont ceux de la boîte *projetée* : on fabrique donc, pour
        chacun, le couple (latitude, longitude) qui s'y projette.
        """
        coins = {
            "sud-ouest": (self.x_min, self.y_min, MARGE_X, VUE_HAUTEUR - MARGE_Y),
            "sud-est": (self.x_max, self.y_min, VUE_LARGEUR - MARGE_X, VUE_HAUTEUR - MARGE_Y),
            "nord-ouest": (self.x_min, self.y_max, MARGE_X, MARGE_Y),
            "nord-est": (self.x_max, self.y_max, VUE_LARGEUR - MARGE_X, MARGE_Y),
        }
        for nom, (x_degres, y_degres, x_attendu, y_attendu) in coins.items():
            latitude = self.latitude_de(y_degres)
            longitude = self.longitude_de(x_degres, latitude)
            with self.subTest(coin=nom):
                x, y = projeter(latitude, longitude, self.cadre)
                self.assertAlmostEqual(x, x_attendu, places=1)
                self.assertAlmostEqual(y, y_attendu, places=1)

    def test_un_point_central_tombe_au_centre_du_dessin(self):
        latitude = (self.y_min + self.y_max) / 2
        longitude = self.longitude_de((self.x_min + self.x_max) / 2, latitude)
        x, y = projeter(latitude, longitude, self.cadre)
        self.assertAlmostEqual(x, VUE_LARGEUR / 2, places=1)
        self.assertAlmostEqual(y, VUE_HAUTEUR / 2, places=1)

    def test_le_nord_a_un_y_plus_petit_que_le_sud(self):
        """L'axe des ordonnées d'un SVG descend : le nord doit être en haut.

        Cotonou est au sud, Parakou au nord, à presque trois degrés de là.
        """
        _x, y_cotonou = projeter(6.3703, 2.3912, self.cadre)
        _x, y_parakou = projeter(9.3372, 2.6303, self.cadre)
        self.assertLess(y_parakou, y_cotonou)

    def test_l_est_a_un_x_plus_grand_que_l_ouest(self):
        _x_ouest, _y = projeter(6.6389, 1.7167, self.cadre)  # Lokossa
        x_est, _y = projeter(6.4969, 2.6283, self.cadre)  # Porto-Novo
        self.assertGreater(x_est, _x_ouest)

    def test_tous_les_points_tiennent_dans_le_viewbox_avec_leur_marge(self):
        """Aucun tracé ne doit sortir du cadre, ni toucher le bord."""
        for latitude, longitude in tous_les_points():
            x, y = projeter(latitude, longitude, self.cadre)
            with self.subTest(point=(latitude, longitude)):
                self.assertGreaterEqual(x, MARGE_X - 0.01)
                self.assertLessEqual(x, VUE_LARGEUR - MARGE_X + 0.01)
                self.assertGreaterEqual(y, MARGE_Y - 0.01)
                self.assertLessEqual(y, VUE_HAUTEUR - MARGE_Y + 0.01)

    def test_la_correction_cosinus_est_prise_point_par_point(self):
        """Et non sur une latitude moyenne commune.

        Avec une constante, la correction serait absorbée par la mise à
        l'échelle qui suit et n'aurait aucun effet observable. Point par point,
        un même écart de longitude donne un écart de x plus petit au nord qu'au
        sud — c'est précisément ce que la correction signifie.
        """
        ecart_au_sud = projeter_degres(6.4, 2.5)[0] - projeter_degres(6.4, 2.0)[0]
        ecart_au_nord = projeter_degres(9.3, 2.5)[0] - projeter_degres(9.3, 2.0)[0]
        self.assertGreater(ecart_au_sud, ecart_au_nord)

    def test_un_cadre_plat_centre_au_lieu_de_diviser_par_zero(self):
        """Un seul point, ou tous alignés : cas limite, pas erreur."""
        unique = Itineraire(
            code="essai", libelle="Essai", etapes=(("Cotonou", 6.3703, 2.3912),)
        )
        cadre = cadre_projection([unique])
        x, y = projeter(6.3703, 2.3912, cadre)
        self.assertAlmostEqual(x, VUE_LARGEUR / 2, places=1)
        self.assertAlmostEqual(y, VUE_HAUTEUR / 2, places=1)

    def test_le_cadre_est_calcule_si_on_ne_le_fournit_pas(self):
        self.assertEqual(projeter(6.3703, 2.3912), projeter(6.3703, 2.3912, self.cadre))


class TraitDeCoteTest(SimpleTestCase):
    """Le golfe est un rectangle, et aucune ville n'est dedans."""

    def test_aucun_point_d_itineraire_n_est_dans_l_eau(self):
        """Le test qui justifie que la côte soit calculée et non placée à l'œil.

        L'eau commence sous `latitude_cote()`, c'est-à-dire sous un y plus
        grand une fois projetée. Chaque point d'itinéraire doit donc avoir un y
        strictement plus petit.
        """
        cadre = cadre_projection()
        _x, y_cote = projeter(latitude_cote(), 2.0, cadre)
        for latitude, longitude in tous_les_points():
            _x, y = projeter(latitude, longitude, cadre)
            with self.subTest(point=(latitude, longitude)):
                self.assertLess(y, y_cote)

    def test_la_cote_passe_sous_la_ville_la_plus_meridionale(self):
        plus_au_sud = min(latitude for latitude, _lon in tous_les_points())
        self.assertLess(latitude_cote(), plus_au_sud)

    def test_l_eau_reste_dans_le_dessin(self):
        """Une côte hors du viewBox ne dessinerait aucune eau du tout."""
        _x, y_cote = projeter(latitude_cote(), 2.0)
        self.assertGreater(y_cote, 0)
        self.assertLess(y_cote, VUE_HAUTEUR)

    def test_une_geographie_vide_ne_leve_rien(self):
        self.assertEqual(latitude_cote([]), 0.0)


class EtapesNommeesTest(SimpleTestCase):
    """Les noms de villes sont devenus des données, pas des commentaires."""

    def test_chaque_etape_porte_un_nom(self):
        for code, itineraire in ITINERAIRES.items():
            with self.subTest(itineraire=code):
                self.assertEqual(len(itineraire.villes), len(itineraire.points))
                for nom in itineraire.villes:
                    self.assertTrue(nom.strip())

    def test_points_reste_une_suite_de_couples(self):
        """Toute la simulation s'en sert : elle n'a pas à connaître les noms."""
        for code, itineraire in ITINERAIRES.items():
            with self.subTest(itineraire=code):
                for point in itineraire.points:
                    self.assertEqual(len(point), 2)

    def test_les_trois_axes_partent_de_cotonou(self):
        """Ce que les noms permettent enfin de vérifier.

        C'était vrai avant aussi, mais seul un commentaire le disait : rien ne
        l'empêchait de cesser de l'être.
        """
        for code, itineraire in ITINERAIRES.items():
            with self.subTest(itineraire=code):
                self.assertEqual(itineraire.villes[0], "Cotonou")

    def test_la_derniere_etape_est_la_destination_du_libelle(self):
        for code, itineraire in ITINERAIRES.items():
            with self.subTest(itineraire=code):
                self.assertIn(itineraire.villes[-1], itineraire.libelle)
