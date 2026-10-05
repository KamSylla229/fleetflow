"""Tests de la page Carte et de son endpoint de positions.

Cinq choses y sont éprouvées, dans cet ordre d'importance :

1. **l'endpoint est fermé** aux visiteurs anonymes. Les positions d'une flotte
   sont la donnée la plus sensible du projet : elles disent où se trouve la
   marchandise ;
2. **la page vit sans la carte**. La liste latérale est rendue par Django,
   avec plaques, états, vitesses et heures. Si Leaflet ne charge pas, l'écran
   reste utilisable — et c'est ce qui compte pour des camions qui roulent là
   où la liaison est mauvaise ;
3. **les icônes par défaut de Leaflet ne sont pas utilisées**, le piège
   annoncé : elles réclament marker-icon.png et marker-shadow.png par des
   chemins que Django ne sert pas là où Leaflet les cherche ;
4. le budget de requêtes de tout l'endpoint, à 5 puis à 26 camions ;
5. les tracés d'itinéraires ne coûtent aucune requête, et ne sont pas
   réexpédiés par le rafraîchissement.
"""

import json
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleet import services
from fleet.itineraires import CENTRE_FLOTTE, ITINERAIRES, ZOOM_FLOTTE
from fleet.models import FournisseurGPS, Mission, PositionGPS, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule
from fleet.views import _itineraires_carte

COTONOU = (6.3703, 2.3912)
BOHICON = (7.1781, 2.0667)


def placer(vehicule, point, *, minutes_avant=1, vitesse="60"):
    """Un relevé GPS pour ce camion, daté de `minutes_avant` minutes."""
    return PositionGPS.objects.create(
        vehicule=vehicule,
        latitude=Decimal(str(point[0])),
        longitude=Decimal(str(point[1])),
        vitesse_kmh=Decimal(vitesse),
        horodatage=timezone.now() - timedelta(minutes=minutes_avant),
    )


def creer_flotte(nombre):
    """Une flotte active, positionnée, sur un itinéraire connu.

    Appelable deux fois dans le même test pour agrandir la flotte, d'où les
    immatriculations indexées sur le nombre de camions déjà en base.
    """
    fournisseur, _ = FournisseurGPS.objects.get_or_create(nom="Cartrack")
    rang_depart = Vehicule.objects.count()
    for index in range(nombre):
        vehicule = creer_vehicule(
            immatriculation=f"CT {1000 + rang_depart + index} RB",
            fournisseur_gps=fournisseur,
            boitier_id=f"CT-{rang_depart + index}",
            itineraire="cotonou_parakou",
            progression=0.4,
        )
        placer(vehicule, COTONOU)


class AccesALaCarteTest(TestCase):
    """La page et l'endpoint sont fermés, et ne laissent rien filtrer."""

    @classmethod
    def setUpTestData(cls):
        creer_flotte(2)

    def test_un_anonyme_est_redirige_sur_la_page(self):
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertEqual(reponse.status_code, 302)
        self.assertTrue(reponse["Location"].startswith(reverse("login")))

    def test_un_anonyme_est_redirige_sur_l_endpoint(self):
        reponse = self.client.get(reverse("fleet:carte_positions"))
        self.assertEqual(reponse.status_code, 302)
        self.assertTrue(reponse["Location"].startswith(reverse("login")))

    def test_un_anonyme_n_obtient_aucune_position(self):
        """Une position est la donnée la plus sensible du projet."""
        reponse = self.client.get(reverse("fleet:carte_positions"))
        contenu = reponse.content.decode("utf-8")
        for marqueur in ("camions", "CT 1000 RB", "6.3703", "latitude"):
            with self.subTest(marqueur=marqueur):
                self.assertNotIn(marqueur, contenu)

    def test_un_utilisateur_connecte_obtient_du_json(self):
        utilisateur = get_user_model().objects.create_user(username="gestionnaire")
        self.client.force_login(utilisateur)
        reponse = self.client.get(reverse("fleet:carte_positions"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse["Content-Type"], "application/json")
        charge = json.loads(reponse.content)
        self.assertEqual(len(charge["camions"]), 2)
        self.assertIn("compteurs", charge)
        self.assertIn("mesure_le", charge)


class PositionsTest(TestCase):
    """Ce que l'endpoint dit de chaque camion."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def positions(self):
        return json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )

    def test_un_camion_en_route_porte_sa_position_et_sa_vitesse(self):
        vehicule = creer_vehicule(immatriculation="CT 0001 RB")
        placer(vehicule, BOHICON, vitesse="62")

        camion = self.positions()["camions"][0]
        self.assertEqual(camion["immatriculation"], "CT 0001 RB")
        self.assertAlmostEqual(camion["lat"], BOHICON[0], places=4)
        self.assertAlmostEqual(camion["lon"], BOHICON[1], places=4)
        self.assertEqual(camion["statut"], services.GPS_EN_ROUTE)
        self.assertEqual(camion["ton"], "vert")
        self.assertEqual(camion["vitesse"], 62)
        self.assertEqual(camion["url"], vehicule.get_absolute_url())

    def test_un_camion_a_l_arret_est_ambre(self):
        placer(creer_vehicule(immatriculation="CT 0002 RB"), COTONOU, vitesse="0")
        camion = self.positions()["camions"][0]
        self.assertEqual(camion["statut"], services.GPS_A_ARRET)
        self.assertEqual(camion["ton"], "ambre")
        self.assertEqual(camion["vitesse"], 0)

    def test_un_camion_sans_releve_reste_dans_la_liste_sans_coordonnees(self):
        """Il ne peut pas être placé, mais c'est lui qu'on cherche.

        Le faire disparaître de l'écran serait le pire des affichages : un
        camion dont le boîtier n'a jamais parlé est une anomalie, pas un
        camion de moins.
        """
        creer_vehicule(immatriculation="CT 0003 RB")
        camion = self.positions()["camions"][0]
        self.assertIsNone(camion["lat"])
        self.assertIsNone(camion["lon"])
        self.assertIsNone(camion["vitesse"])
        self.assertIsNone(camion["vu_le"])
        self.assertEqual(camion["statut"], services.GPS_AUCUNE_DONNEE)
        self.assertEqual(camion["ton"], "neutre")

    def test_un_boitier_muet_est_rouge_et_garde_sa_derniere_position(self):
        vehicule = creer_vehicule(immatriculation="CT 0004 RB")
        minutes = services.seuil_sans_signal().total_seconds() / 60
        placer(vehicule, BOHICON, minutes_avant=minutes + 10)

        camion = self.positions()["camions"][0]
        self.assertEqual(camion["statut"], services.GPS_SANS_SIGNAL)
        self.assertEqual(camion["ton"], "rouge")
        # La dernière position connue reste affichée : c'est là qu'il faut
        # aller chercher le camion.
        self.assertAlmostEqual(camion["lat"], BOHICON[0], places=4)

    def test_un_signal_coupe_est_distingue_d_une_panne(self):
        vehicule = creer_vehicule(immatriculation="CT 0005 RB", signal_coupe=True)
        placer(vehicule, COTONOU)
        self.assertTrue(self.positions()["camions"][0]["signal_coupe"])

    def test_la_mission_en_cours_accompagne_le_camion(self):
        vehicule = creer_vehicule(immatriculation="CT 0006 RB", progression=0.42)
        placer(vehicule, COTONOU)
        Mission.objects.create(
            vehicule=vehicule,
            chauffeur=creer_chauffeur(),
            depart="Cotonou",
            destination="Parakou",
            date_depart=timezone.localdate(),
            km_depart=100_000,
            statut=Mission.Statut.EN_COURS,
        )
        camion = self.positions()["camions"][0]
        self.assertEqual(camion["destination"], "Parakou")
        self.assertEqual(camion["chauffeur"], "Rodrigue Hounkpatin")
        self.assertEqual(camion["progression"], 42)

    def test_un_camion_desactive_quitte_la_carte(self):
        placer(creer_vehicule(immatriculation="CT 0007 RB", actif=False), COTONOU)
        self.assertEqual(self.positions()["camions"], [])

    def test_les_compteurs_couvrent_les_quatre_etats(self):
        placer(creer_vehicule(immatriculation="CT 0010 RB"), COTONOU, vitesse="60")
        placer(creer_vehicule(immatriculation="CT 0011 RB"), COTONOU, vitesse="0")
        creer_vehicule(immatriculation="CT 0012 RB")

        compteurs = self.positions()["compteurs"]
        self.assertEqual(compteurs[services.GPS_EN_ROUTE], 1)
        self.assertEqual(compteurs[services.GPS_A_ARRET], 1)
        self.assertEqual(compteurs[services.GPS_AUCUNE_DONNEE], 1)
        self.assertEqual(compteurs[services.GPS_SANS_SIGNAL], 0)
        # Les quatre codes sont toujours présents, même à zéro : le gabarit
        # peut les afficher sans {% if %}, et le script les récrire sans se
        # demander si la clé existe.
        self.assertEqual(
            sorted(compteurs), sorted(services.LIBELLES_STATUT_GPS)
        )


class LaPageVitSansLaCarteTest(TestCase):
    """La liste latérale est rendue par Django, pas par Leaflet."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicule = creer_vehicule(
            immatriculation="CT 0100 RB", itineraire="cotonou_parakou"
        )
        placer(cls.vehicule, BOHICON, vitesse="58")

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_la_page_repond_et_marque_le_bon_onglet(self):
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.context["nav_actif"], "carte")

    def test_la_liste_est_dans_le_html_servi(self):
        """Sans exécuter une ligne de JavaScript."""
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertContains(reponse, "CT 0100 RB")
        self.assertContains(reponse, "58 km/h")
        self.assertContains(reponse, "En route")
        self.assertContains(reponse, "ff-marqueur__point--vert")

    def test_l_entree_de_menu_est_devenue_un_lien(self):
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertContains(reponse, f'href="{reverse("fleet:carte")}"')
        contenu = reponse.content.decode("utf-8")
        debut = contenu.index(">Carte<")
        self.assertNotIn("bientôt", contenu[debut - 200 : debut + 200])

    def test_les_positions_initiales_voyagent_avec_la_page(self):
        """Pas de carte vide le temps d'un aller-retour, et pas de requête de plus."""
        reponse = self.client.get(reverse("fleet:carte"))
        charge = self.lire_json_script(reponse, "carte-camions")
        self.assertEqual(len(charge), 1)
        self.assertAlmostEqual(charge[0]["lat"], BOHICON[0], places=4)

    def test_le_centre_et_le_zoom_viennent_du_module_de_geographie(self):
        """Et non de constantes écrites dans le JavaScript.

        Sans eux, Leaflet ouvrirait sur le golfe de Guinée, à la latitude et
        longitude zéro.
        """
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertEqual(
            self.lire_json_script(reponse, "carte-centre"), list(CENTRE_FLOTTE)
        )
        self.assertContains(reponse, f'data-zoom="{ZOOM_FLOTTE}"')

    def test_la_page_et_le_json_decrivent_les_memes_camions(self):
        """Une seule fonction les produit : views._camions_carte.

        Si ce test tombe, c'est que les deux chemins se sont séparés — et
        l'utilisateur verrait ses camions changer de forme ou de place dix
        secondes après l'ouverture de la page.
        """
        page = self.lire_json_script(
            self.client.get(reverse("fleet:carte")), "carte-camions"
        )
        endpoint = json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )["camions"]
        # mesure_le n'est pas dans ces dictionnaires : les deux listes doivent
        # donc être identiques champ par champ.
        self.assertEqual(page, endpoint)

    def lire_json_script(self, reponse, identifiant):
        contenu = reponse.content.decode("utf-8")
        trouve = re.search(
            r'id="' + identifiant + r'"[^>]*>(.*?)</script>', contenu, re.S
        )
        self.assertIsNotNone(trouve, f"balise {identifiant} absente")
        return json.loads(trouve.group(1))


class RessourcesDeLaCarteTest(TestCase):
    """Leaflet en local, et les icônes par défaut laissées de côté."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        placer(creer_vehicule(immatriculation="CT 0200 RB"), COTONOU)

    def test_leaflet_est_servi_depuis_static(self):
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertContains(reponse, "vendor/leaflet/leaflet.js")
        self.assertContains(reponse, "vendor/leaflet/leaflet.css")
        for chemin in ("vendor/leaflet/leaflet.js", "js/carte.js"):
            with self.subTest(chemin=chemin):
                self.assertIsNotNone(finders.find(chemin))

    def test_les_tuiles_sont_la_seule_ressource_distante(self):
        """L'exception assumée, et elle doit rester visible.

        Un fond de carte ne peut pas être local : les tuiles du Bénin
        représenteraient plusieurs gigaoctets. Le reste de la page — Leaflet,
        le thème, les polices — est servi depuis static/. Ce test nomme
        l'exception pour qu'une seconde ne s'ajoute pas en silence.
        """
        contenu = self.client.get(reverse("fleet:carte")).content.decode("utf-8")
        self.assertIn("tile.openstreetmap.org", contenu)

        distants = set(re.findall(r'https?://([^/"\')\s]+)', contenu))
        self.assertEqual(distants, {"tile.openstreetmap.org"})

    def test_les_icones_par_defaut_de_leaflet_ne_sont_pas_utilisees(self):
        """Le piège annoncé : elles donnent des 404 sous Django.

        Leaflet cherche marker-icon.png à côté de son propre fichier CSS, par
        un chemin qu'il devine. Sous Django, avec un préfixe /static/ et un
        jour un nom haché par collectstatic, cette devinette tombe à côté. Le
        projet pose donc des divIcon colorés par la feuille de style — aucune
        image, aucun chemin à devisser.
        """
        source = open(
            finders.find("js/carte.js"), encoding="utf-8"
        ).read()
        self.assertIn("divIcon", source)
        for interdit in ("iconUrl", "shadowUrl", "Icon.Default", "iconRetinaUrl"):
            with self.subTest(interdit=interdit):
                self.assertNotIn(interdit, source)

    def test_le_script_ne_pose_jamais_de_html_venu_du_serveur(self):
        """textContent et createElement seulement : les plaques sont des saisies.

        Le seul innerHTML tolérable serait une chaîne constante ; il n'y en a
        aucun, et l'`html` du divIcon est construit à partir d'un nom de ton
        validé contre une liste de quatre valeurs.
        """
        source = open(finders.find("js/carte.js"), encoding="utf-8").read()
        # « .innerHTML » et non « innerHTML » : le commentaire d'en-tête du
        # script explique justement pourquoi on ne s'en sert pas, et chercher
        # le mot nu faisait échouer ce test sur sa propre documentation.
        self.assertNotIn(".innerHTML", source)
        self.assertIn("textContent", source)

    def test_le_script_ne_recadre_pas_la_carte_a_chaque_passage(self):
        """Sinon la vue sauterait toutes les dix secondes sous le doigt.

        Le test regarde la source parce que le symptôme est invisible
        autrement : la page répond 200 et les points sont justes dans les deux
        cas. Seul le garde-fou `cadrageFait` distingue un cadrage de courtoisie
        d'une carte qu'on arrache des mains de l'utilisateur.
        """
        source = open(finders.find("js/carte.js"), encoding="utf-8").read()
        self.assertIn("cadrageFait", source)
        self.assertEqual(source.count("fitBounds"), 1)


class ItinerairesTracesTest(TestCase):
    """Les polylignes : aucune requête, et seulement les axes parcourus."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_seuls_les_axes_parcourus_sont_envoyes(self):
        vehicule = creer_vehicule(
            immatriculation="CT 0300 RB", itineraire="cotonou_lokossa"
        )
        placer(vehicule, COTONOU)

        reponse = self.client.get(reverse("fleet:carte"))
        traces = reponse.context["itineraires"]
        self.assertEqual([trace["code"] for trace in traces], ["cotonou_lokossa"])
        # Les trois existent dans fleet/itineraires.py : c'est bien un choix
        # et non une absence de données.
        self.assertEqual(len(ITINERAIRES), 3)

    def test_un_camion_sans_itineraire_n_ajoute_aucune_trace(self):
        placer(creer_vehicule(immatriculation="CT 0301 RB"), COTONOU)
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertEqual(reponse.context["itineraires"], [])

    def test_les_traces_ne_coutent_aucune_requete(self):
        """Elles viennent d'un module Python, pas de la base.

        C'est la leçon de la septième requête du tableau de bord appliquée
        d'avance : les codes d'itinéraire sont lus dans la liste de camions
        déjà chargée, et non redemandés à la base.
        """
        camions = [
            {"itineraire": "cotonou_parakou"},
            {"itineraire": "cotonou_parakou"},
            {"itineraire": ""},
        ]
        with self.assertNumQueries(0):
            traces = _itineraires_carte(camions)
        self.assertEqual([trace["code"] for trace in traces], ["cotonou_parakou"])
        self.assertGreater(len(traces[0]["points"]), 1)

    def test_les_traces_ne_sont_pas_reexpediees_par_le_rafraichissement(self):
        """Une donnée figée ne se paie pas au prix d'une donnée vivante."""
        vehicule = creer_vehicule(
            immatriculation="CT 0302 RB", itineraire="cotonou_parakou"
        )
        placer(vehicule, COTONOU)
        charge = json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )
        self.assertNotIn("itineraires", charge)
        self.assertNotIn("centre", charge)


class BudgetDeRequetesDeLaCarteTest(TestCase):
    """Le coût de tout l'endpoint, pas seulement de la fonction d'assemblage."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        self.adresse = reverse("fleet:carte_positions")

    def test_le_nombre_de_requetes_est_le_meme_a_5_et_a_26_camions(self):
        creer_flotte(5)
        with CaptureQueriesContext(connection) as avec_cinq:
            self.assertEqual(self.client.get(self.adresse).status_code, 200)

        creer_flotte(21)
        self.assertEqual(Vehicule.objects.count(), 26)
        with CaptureQueriesContext(connection) as avec_vingt_six:
            self.assertEqual(self.client.get(self.adresse).status_code, 200)

        self.assertEqual(
            len(avec_cinq),
            len(avec_vingt_six),
            "le nombre de requêtes croît avec la flotte : un N+1 s'est glissé "
            f"({len(avec_cinq)} puis {len(avec_vingt_six)})",
        )

    def test_le_compte_exact_de_l_endpoint(self):
        """Quatre : la flotte annotée, les missions en cours, la session, l'utilisateur.

        Les quatre sous-requêtes GPS d'annoter_dernieres_positions sont
        corrélées : elles sont exécutées *dans* la première requête, pas en
        plus d'elle. Sans elles, afficher la dernière position de chaque camion
        coûterait une requête par ligne.
        """
        creer_flotte(5)
        with self.assertNumQueries(4):
            self.client.get(self.adresse)

    def test_la_page_coute_une_requete_de_plus_que_l_endpoint(self):
        """Celle du bandeau d'alertes, posée par le context processor."""
        creer_flotte(5)
        with CaptureQueriesContext(connection) as endpoint:
            self.client.get(self.adresse)
        with CaptureQueriesContext(connection) as page:
            self.client.get(reverse("fleet:carte"))
        self.assertEqual(len(page), len(endpoint) + 1)
