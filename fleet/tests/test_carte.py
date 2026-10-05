"""Tests de la page Carte et de son endpoint de positions.

Cinq choses y sont éprouvées, dans cet ordre d'importance :

1. **l'endpoint est fermé** aux visiteurs anonymes. Les positions d'une flotte
   sont la donnée la plus sensible du projet : elles disent où se trouve la
   marchandise ;
2. **la page vit sans le dessin**. La liste latérale est rendue par Django,
   avec plaques, états, vitesses et heures. Si le script ne tourne pas,
   l'écran reste utilisable — et c'est ce qui compte pour des camions qui
   roulent là où la liaison est mauvaise ;
3. **le script ne contient ni géographie, ni couleur, ni donnée en dur**. La
   projection vit dans fleet/itineraires.py, les teintes dans fleetflow.css,
   les villes et les axes dans les balises json_script. Trois tests lisent la
   source pour s'en assurer, parce que rien d'autre ne le verrait ;
4. le budget de requêtes de tout l'endpoint, à 5 puis à 26 camions ;
5. les tracés d'itinéraires ne coûtent aucune requête, et ne sont pas
   réexpédiés par le rafraîchissement.
"""

import datetime
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
from fleet.itineraires import ITINERAIRES, VUE_HAUTEUR, VUE_LARGEUR
from fleet.models import FournisseurGPS, Mission, PositionGPS, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule
from fleet.views import _camions_carte, _geometrie_carte

COTONOU = (6.3703, 2.3912)
BOHICON = (7.1781, 2.0667)


def code_du_script(nom):
    """Le code d'un fichier .js, commentaires retirés.

    Plusieurs tests de ce fichier interdisent au script de contenir certains
    mots — « latitude », une couleur. Sans ce filtre, ils échouent sur la
    documentation du script lui-même, qui explique précisément pourquoi il ne
    contient pas ces choses. Un test qui tombe sur le commentaire qui le
    justifie ne prouve rien.

    Le découpage est naïf : il ne survivrait pas à une chaîne JavaScript
    contenant les deux caractères d'ouverture d'un commentaire. Il n'y en a
    pas, et le jour où il y en aura une, c'est ce test qui le dira.
    """
    texte = open(finders.find(nom), encoding="utf-8").read()
    sans_blocs = re.sub(r"/\*.*?\*/", " ", texte, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", sans_blocs)


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
    """La liste latérale est rendue par Django, pas par le script."""

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

    def test_la_geometrie_arrive_deja_projetee(self):
        """Le script ne reçoit aucune latitude : seulement des x et des y.

        C'est la règle qui garde la projection à un seul endroit. Si elle
        tombe, c'est que quelqu'un a remis des coordonnées géographiques dans
        la page, et la formule finira par exister en deux exemplaires —
        Python et JavaScript — qui divergeront.
        """
        reponse = self.client.get(reverse("fleet:carte"))
        geometrie = self.lire_json_script(reponse, "carte-geometrie")

        self.assertEqual(geometrie["largeur"], VUE_LARGEUR)
        self.assertEqual(geometrie["hauteur"], VUE_HAUTEUR)

        for trace in geometrie["itineraires"]:
            for x, y in trace["points"]:
                with self.subTest(point=(x, y)):
                    self.assertGreaterEqual(x, 0)
                    self.assertLessEqual(x, VUE_LARGEUR)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(y, VUE_HAUTEUR)

    def test_les_villes_sont_nommees_et_dedoublonnees(self):
        """Cotonou est le départ des trois axes, et n'est dessinée qu'une fois."""
        reponse = self.client.get(reverse("fleet:carte"))
        geometrie = self.lire_json_script(reponse, "carte-geometrie")
        noms = [ville["nom"] for ville in geometrie["villes"]]

        self.assertIn("Cotonou", noms)
        self.assertEqual(len(noms), len(set(noms)))
        self.assertEqual(noms, sorted(noms))

    def test_le_trait_de_cote_est_dans_le_dessin(self):
        """Une côte hors du cadre ne dessinerait aucune eau du tout."""
        reponse = self.client.get(reverse("fleet:carte"))
        geometrie = self.lire_json_script(reponse, "carte-geometrie")
        self.assertGreater(geometrie["cote_y"], 0)
        self.assertLess(geometrie["cote_y"], VUE_HAUTEUR)

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
    """Aucune bibliothèque, aucune couleur et aucune géographie dans le script."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        placer(creer_vehicule(immatriculation="CT 0200 RB"), COTONOU)

    def test_le_dessin_ne_charge_aucune_bibliotheque(self):
        """Un SVG écrit à la main a remplacé Leaflet, 162 Ko et ses contraintes."""
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertContains(reponse, "js/carte.js")
        self.assertNotContains(reponse, "leaflet")
        self.assertIsNotNone(finders.find("js/carte.js"))

    def test_la_page_ne_contacte_aucun_service_exterieur(self):
        """Plus aucun hôte distant, et plus aucun réglage pour y arriver.

        La version Leaflet dépendait d'un serveur de tuiles — qui a fini par
        renvoyer « Access blocked », sa politique d'usage interdisant ce genre
        de dépendance. Le dessin maison a supprimé la question : il n'y a plus
        rien à configurer, et la Definition of Done « Wi-Fi coupé, tout tient »
        est vraie sans condition.
        """
        contenu = self.client.get(reverse("fleet:carte")).content.decode("utf-8")
        distants = set(re.findall(r'https?://([^/"\')\s]+)', contenu))
        self.assertEqual(distants, set())

    def test_le_script_ne_contient_aucune_geographie(self):
        """La projection n'existe qu'à un endroit : fleet/itineraires.py.

        Le script reçoit des x et des y. S'il se mettait à manipuler des
        latitudes, la formule existerait en deux exemplaires, dans deux
        langages — c'est la faute que le projet évite depuis le formatage des
        nombres du tableau de bord.
        """
        source = code_du_script("js/carte.js")
        for interdit in ("latitude", "cos(", "Math.cos", "6.37", "2.39"):
            with self.subTest(interdit=interdit):
                self.assertNotIn(interdit, source)

    def test_le_script_ne_contient_aucune_couleur(self):
        """Toutes les teintes sont dans fleetflow.css, section 18 quater.

        Une couleur écrite dans le script serait invisible au moment de
        retoucher le thème : on corrigerait la feuille de style et le dessin
        garderait l'ancienne teinte, sans que rien ne le signale.
        """
        source = code_du_script("js/carte.js")
        for interdit in ("#", "rgb(", "rgba(", "fill:", "stroke:"):
            with self.subTest(interdit=interdit):
                self.assertNotIn(interdit, source)

    def test_le_script_ne_pose_jamais_de_html_venu_du_serveur(self):
        """textContent et createElement seulement : les plaques sont des saisies.

        Le seul innerHTML tolérable serait une chaîne constante ; il n'y en a
        aucun, et l'`html` du divIcon est construit à partir d'un nom de ton
        validé contre une liste de quatre valeurs.
        """
        source = code_du_script("js/carte.js")
        # « .innerHTML » et non « innerHTML » : le commentaire d'en-tête du
        # script explique justement pourquoi on ne s'en sert pas, et chercher
        # le mot nu faisait échouer ce test sur sa propre documentation.
        self.assertNotIn(".innerHTML", source)
        self.assertIn("textContent", source)

    def test_le_cadrage_ne_bouge_jamais(self):
        """Il est calculé sur les itinéraires, donc identique à chaque rendu.

        La version Leaflet avait besoin d'un garde-fou pour ne pas recentrer
        la carte toutes les dix secondes sous le doigt de l'utilisateur. Le
        dessin SVG n'en a pas besoin : le cadre vient des axes, pas des
        camions, et un camion qui avance ne le déplace pas. Le problème a
        disparu au lieu d'être surveillé.
        """
        avant = self.client.get(reverse("fleet:carte")).context["geometrie"]
        placer(creer_vehicule(immatriculation="CT 0201 RB"), BOHICON)
        apres = self.client.get(reverse("fleet:carte")).context["geometrie"]
        self.assertEqual(avant["itineraires"], apres["itineraires"])
        self.assertEqual(avant["cote_y"], apres["cote_y"])

    def test_le_premier_affichage_ne_fait_pas_glisser_les_camions(self):
        """Sans cette classe, un camion qui apparaît glisse depuis le coin.

        Invisible pour un test de vue — la page répond 200 dans les deux cas —,
        d'où une lecture de la source.
        """
        source = code_du_script("js/carte.js")
        self.assertIn("ff-camion--pose", source)
        # style.transform et non l'attribut SVG, qui ne s'anime pas.
        self.assertIn("style.transform", source)


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
        traces = reponse.context["geometrie"]["itineraires"]
        self.assertEqual([trace["code"] for trace in traces], ["cotonou_lokossa"])
        # Les trois existent dans fleet/itineraires.py : c'est bien un choix
        # et non une absence de données.
        self.assertEqual(len(ITINERAIRES), 3)

    def test_un_camion_sans_itineraire_n_ajoute_aucune_trace(self):
        placer(creer_vehicule(immatriculation="CT 0301 RB"), COTONOU)
        reponse = self.client.get(reverse("fleet:carte"))
        self.assertEqual(reponse.context["geometrie"]["itineraires"], [])

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
            geometrie = _geometrie_carte(camions)
        traces = geometrie["itineraires"]
        self.assertEqual([trace["code"] for trace in traces], ["cotonou_parakou"])
        self.assertGreater(len(traces[0]["points"]), 1)
        self.assertEqual(len(geometrie["villes"]), len(traces[0]["points"]))

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
        self.assertNotIn("geometrie", charge)
        self.assertNotIn("villes", charge)


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


class AgeDuDernierContactTest(TestCase):
    """Le compteur « il y a 2 h 59 » des boîtiers muets.

    L'âge est mesuré **par le serveur**, à l'instant de la requête. Le
    navigateur n'y ajoute que le temps écoulé depuis la réponse : il ne
    demande jamais l'heure au poste de l'utilisateur, qui peut être faux de
    plusieurs heures sans que personne ne s'en doute. Une horloge fausse
    s'écoule quand même à la bonne vitesse — c'est tout ce dont le compteur a
    besoin.
    """

    def test_un_contact_a_11h51_vu_a_14h50_donne_10740_secondes(self):
        vehicule = creer_vehicule(immatriculation="CT 0400 RB")
        contact = timezone.make_aware(
            datetime.datetime(2026, 10, 5, 11, 51, 0),
            timezone.get_current_timezone(),
        )
        releve = timezone.make_aware(
            datetime.datetime(2026, 10, 5, 14, 50, 0),
            timezone.get_current_timezone(),
        )
        PositionGPS.objects.create(
            vehicule=vehicule,
            latitude=Decimal(str(COTONOU[0])),
            longitude=Decimal(str(COTONOU[1])),
            vitesse_kmh=Decimal("0"),
            horodatage=contact,
        )

        camion = _camions_carte(maintenant=releve)[0]
        # 2 h 59 min, soit 179 minutes.
        self.assertEqual(camion["age_secondes"], 10_740)
        self.assertEqual(camion["statut"], services.GPS_SANS_SIGNAL)

    def test_un_camion_sans_releve_n_a_pas_d_age(self):
        """None, et non zéro : « jamais vu » n'est pas « vu à l'instant »."""
        creer_vehicule(immatriculation="CT 0401 RB")
        self.assertIsNone(_camions_carte()[0]["age_secondes"])

    def test_le_script_n_interroge_pas_l_horloge_du_poste(self):
        """Il lit l'écoulement local, jamais une date absolue du navigateur.

        Date.now() sert à mesurer un écart entre deux instants du même poste,
        ce qui reste juste même si l'horloge est déréglée. Lire l'heure du
        navigateur pour la comparer à celle du serveur serait l'erreur.
        """
        source = code_du_script("js/carte.js")
        self.assertIn("instantReponse", source)
        self.assertNotIn("new Date(", source)


class PositionDansLeDessinTest(TestCase):
    """Les camions arrivent avec leur place dans le dessin, pas à projeter."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_un_camion_positionne_porte_un_x_et_un_y(self):
        placer(creer_vehicule(immatriculation="CT 0500 RB"), BOHICON)
        camion = json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )["camions"][0]
        self.assertGreaterEqual(camion["x"], 0)
        self.assertLessEqual(camion["x"], VUE_LARGEUR)
        self.assertGreaterEqual(camion["y"], 0)
        self.assertLessEqual(camion["y"], VUE_HAUTEUR)

    def test_un_camion_sans_releve_n_a_pas_de_place(self):
        creer_vehicule(immatriculation="CT 0501 RB")
        camion = json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )["camions"][0]
        self.assertIsNone(camion["x"])
        self.assertIsNone(camion["y"])

    def test_le_nord_a_un_y_plus_petit_que_le_sud(self):
        """Le même sens que dans le dessin : l'axe des ordonnées descend."""
        placer(creer_vehicule(immatriculation="CT 0502 RB"), COTONOU)
        placer(creer_vehicule(immatriculation="CT 0503 RB"), (9.3372, 2.6303))
        camions = json.loads(
            self.client.get(reverse("fleet:carte_positions")).content
        )["camions"]
        par_plaque = {camion["immatriculation"]: camion for camion in camions}
        self.assertLess(
            par_plaque["CT 0503 RB"]["y"], par_plaque["CT 0502 RB"]["y"]
        )
