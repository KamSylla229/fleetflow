"""Tests de la page Tableau de bord et de son endpoint JSON.

Quatre choses y sont éprouvées, dans cet ordre d'importance :

1. **l'endpoint est fermé** aux visiteurs anonymes, et ne laisse fuir aucun
   chiffre. Un oubli de ce genre n'échoue jamais : il ouvre discrètement une
   porte, et seul un test qui essaie sans être connecté s'en aperçoit ;
2. **la page et le JSON écrivent les mêmes chaînes**. C'est le piège propre à
   un écran rafraîchi : si Django rend « 13 584 » et que le script écrit
   « 13584 » dix secondes plus tard, le chiffre saute sous les yeux de
   l'utilisateur sans que rien ne soit faux ;
3. le budget de requêtes de **tout** l'endpoint — pas seulement de
   calculer_kpi — mesuré à 5 puis à 26 camions ;
4. l'ossature : la page répond, porte le bon onglet, et l'entrée de menu
   n'annonce plus « bientôt ».
"""

import json
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleet import services
from fleet.kpi import calculer_kpi
from fleet.models import (
    Alerte,
    Document,
    FournisseurGPS,
    PleinCarburant,
    PositionGPS,
    Vehicule,
)
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule

# Deux points sur la RNIE 2, à une centaine de kilomètres l'un de l'autre.
COTONOU = (6.3703, 2.3912)
BOHICON = (7.1781, 2.0667)


def creer_flotte(nombre):
    """Une flotte complète : documents, relevés GPS et pleins appariables.

    Appelable deux fois dans le même test pour agrandir la flotte, d'où le
    get_or_create du fournisseur et les immatriculations indexées sur le
    nombre de camions déjà en base.
    """
    fournisseur, _ = FournisseurGPS.objects.get_or_create(nom="Cartrack")
    rang_depart = Vehicule.objects.count()
    chauffeur = creer_chauffeur(
        nom=f"Chauffeur {rang_depart}", permis=f"BJ-TB-{rang_depart}"
    )
    aujourdhui = timezone.localdate()
    maintenant = timezone.now()

    for index in range(nombre):
        vehicule = creer_vehicule(
            immatriculation=f"TB {1000 + rang_depart + index} RB",
            fournisseur_gps=fournisseur,
            boitier_id=f"CT-TB-{rang_depart + index}",
        )
        creer_document(vehicule, Document.TypeDocument.ASSURANCE, jours=20)
        for rang, point in enumerate((COTONOU, BOHICON)):
            PositionGPS.objects.create(
                vehicule=vehicule,
                latitude=Decimal(str(point[0])),
                longitude=Decimal(str(point[1])),
                vitesse_kmh=Decimal("60"),
                horodatage=maintenant - timedelta(minutes=30 - rang * 10),
            )
        for rang, (km, litres) in enumerate(((100_000, "200"), (101_000, "320"))):
            PleinCarburant.objects.create(
                vehicule=vehicule,
                chauffeur=chauffeur,
                date=aujourdhui - timedelta(days=20 - rang * 10),
                litres=Decimal(litres),
                prix_litre=Decimal("715"),
                km_compteur=km,
            )


class AccesAuxIndicateursTest(TestCase):
    """L'endpoint JSON est fermé, et il ne laisse rien filtrer.

    Le premier test de ce fichier, et le plus important : un endpoint de
    données oublié sans décorateur ne lève aucune erreur, ne casse aucune
    page, et publie la facture de carburant d'une PME.
    """

    @classmethod
    def setUpTestData(cls):
        creer_flotte(2)

    def test_un_anonyme_est_redirige_vers_la_connexion(self):
        reponse = self.client.get(reverse("fleet:dashboard_kpi"))
        self.assertEqual(reponse.status_code, 302)
        # settings.LOGIN_URL vaut « login » : un *nom de route*, que Django
        # resout. C'est l'adresse resolue qu'il faut comparer.
        self.assertTrue(reponse["Location"].startswith(reverse("login")))

    def test_un_anonyme_n_obtient_aucune_donnee(self):
        """La redirection ne doit pas charrier les chiffres au passage."""
        reponse = self.client.get(reverse("fleet:dashboard_kpi"))
        contenu = reponse.content.decode("utf-8")
        for marqueur in ("indicateurs", "km_par_camion", "TB 1000 RB"):
            with self.subTest(marqueur=marqueur):
                self.assertNotIn(marqueur, contenu)

    def test_la_page_est_fermee_aussi(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertEqual(reponse.status_code, 302)
        self.assertTrue(reponse["Location"].startswith(reverse("login")))

    def test_un_utilisateur_connecte_obtient_du_json(self):
        utilisateur = get_user_model().objects.create_user(username="gestionnaire")
        self.client.force_login(utilisateur)
        reponse = self.client.get(reverse("fleet:dashboard_kpi"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse["Content-Type"], "application/json")
        charge = json.loads(reponse.content)
        self.assertEqual(len(charge["indicateurs"]), 5)
        self.assertIn("km_par_camion", charge)
        self.assertIn("mesure_le", charge)


class MemesChiffresDansLaPageEtDansLeJsonTest(TestCase):
    """Le rendu initial et le rafraîchissement écrivent **la même chaîne**.

    C'est la raison d'être de views._indicateurs_affichables : les nombres
    sont formatés une seule fois, côté serveur, et le script ne fait que
    remplacer du texte. Si ce test tombe, c'est qu'un formatage est reparti
    dans le gabarit ou dans le JavaScript — et l'utilisateur verrait ses
    chiffres changer de forme dix secondes après l'ouverture de la page.
    """

    @classmethod
    def setUpTestData(cls):
        creer_flotte(3)

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_chaque_indicateur_porte_la_meme_valeur_des_deux_cotes(self):
        page = self.client.get(reverse("fleet:dashboard")).content.decode("utf-8")
        charge = json.loads(
            self.client.get(reverse("fleet:dashboard_kpi")).content
        )

        for indicateur in charge["indicateurs"]:
            with self.subTest(cle=indicateur["cle"]):
                motif = (
                    r'data-kpi="' + re.escape(indicateur["cle"]) + r'">([^<]*)</span>'
                )
                trouve = re.search(motif, page)
                self.assertIsNotNone(
                    trouve,
                    f"la carte {indicateur['cle']} n'est pas dans la page",
                )
                self.assertEqual(trouve.group(1).strip(), indicateur["valeur"])

    def test_les_cinq_cles_attendues_sont_presentes(self):
        """Les clés sont le contrat entre la vue, le gabarit et le script."""
        charge = json.loads(
            self.client.get(reverse("fleet:dashboard_kpi")).content
        )
        self.assertEqual(
            [indicateur["cle"] for indicateur in charge["indicateurs"]],
            [
                "en_service",
                "en_mouvement",
                "km_du_jour",
                "consommation_moy",
                "echeances_60j",
            ],
        )

    def test_les_jauges_portent_les_memes_plaques_que_la_page(self):
        page = self.client.get(reverse("fleet:dashboard")).content.decode("utf-8")
        charge = json.loads(
            self.client.get(reverse("fleet:dashboard_kpi")).content
        )
        self.assertEqual(len(charge["km_par_camion"]), 3)
        for ligne in charge["km_par_camion"]:
            with self.subTest(plaque=ligne["immatriculation"]):
                self.assertIn(ligne["immatriculation"], page)
                self.assertIn(ligne["km"] + " km", page)
                # La largeur est un pourcentage : elle doit tenir dans la
                # piste, sinon la jauge déborde de sa carte.
                self.assertGreaterEqual(ligne["part"], 0)
                self.assertLessEqual(ligne["part"], 100)


class ContenuDeLaPageTest(TestCase):
    """Ce que la page affiche autour des cinq cartes."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicule = creer_vehicule(immatriculation="TB 9001 RB")
        cls.chauffeur = creer_chauffeur()

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_la_page_repond_et_marque_le_bon_onglet(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.context["nav_actif"], "dashboard")
        self.assertContains(reponse, "Tableau de bord")

    def test_l_endpoint_marque_aussi_l_onglet_du_tableau_de_bord(self):
        """« dashboard_kpi » commence par le préfixe « dashboard ».

        Rien ne l'affiche — l'endpoint ne rend aucun gabarit —, mais le
        vérifier documente pourquoi le context processor n'a pas eu besoin
        d'une seconde entrée.
        """
        from fleet.context_processors import PREFIXES_NAVIGATION

        correspondances = [
            cle for prefixe, cle in PREFIXES_NAVIGATION
            if "dashboard_kpi".startswith(prefixe)
        ]
        self.assertEqual(correspondances, ["dashboard"])

    def test_l_entree_de_menu_est_devenue_un_lien(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(reponse, reverse("fleet:dashboard"))
        self.assertContains(reponse, 'aria-current="page"')

    def test_une_echeance_depassee_colore_la_carte_et_apparait_dans_la_liste(self):
        creer_document(
            self.vehicule, Document.TypeDocument.ASSURANCE, jours=-17
        )
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(reponse, "ff-kpi--alerte")
        self.assertContains(reponse, "dont 1 dépassée")
        self.assertContains(reponse, "-17 j")

    def test_sans_echeance_la_carte_reste_neutre(self):
        creer_document(
            self.vehicule, Document.TypeDocument.ASSURANCE, jours=300
        )
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertNotContains(reponse, "ff-kpi--alerte")
        self.assertContains(reponse, "aucune dépassée")
        self.assertContains(reponse, "Rien à renouveler dans le mois.")

    def test_une_alerte_ouverte_apparait_avec_sa_duree(self):
        Alerte.objects.create(
            type_alerte=Alerte.TypeAlerte.SANS_SIGNAL,
            vehicule=self.vehicule,
            message="Boîtier muet depuis 3 h",
        )
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(reponse, "Boîtier muet depuis 3 h")
        # data-depuis est lu par compteur-alerte.js, déjà chargé par
        # base.html : la durée avance sans que cette page ait son propre code.
        self.assertContains(reponse, "data-depuis")

    def test_sans_alerte_la_carte_le_dit(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(reponse, "Aucune alerte ouverte")

    def test_sans_releve_gps_la_liste_des_kilometres_le_dit(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(reponse, "Aucun relevé GPS aujourd'hui")

    def test_la_consommation_inconnue_affiche_un_tiret(self):
        """Et non zéro : « on ne sait pas » n'est pas « ne consomme rien »."""
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertIsNone(calculer_kpi()["consommation_moy"])
        self.assertContains(reponse, "—")
        self.assertContains(reponse, "pas encore deux pleins")

    def test_au_dela_de_huit_echeances_un_lien_renvoie_vers_entretien(self):
        """Le tableau de bord donne l'ordre de grandeur, pas la liste entière."""
        from fleet.views import ECHEANCES_AFFICHEES

        for index in range(ECHEANCES_AFFICHEES + 3):
            vehicule = creer_vehicule(immatriculation=f"TB 91{index:02d} RB")
            creer_document(
                vehicule, Document.TypeDocument.ASSURANCE, jours=index + 1
            )

        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertEqual(len(reponse.context["echeances"]), ECHEANCES_AFFICHEES)
        self.assertEqual(reponse.context["echeances_restantes"], 3)
        self.assertContains(reponse, "Voir les 3 autres")
        self.assertContains(reponse, reverse("fleet:entretien_liste"))


class BudgetDeRequetesDeLEndpointTest(TestCase):
    """Le coût de **tout** l'endpoint, pas seulement de calculer_kpi.

    Mesurer la fonction seule laisserait passer une requête ajoutée par la
    vue, par un middleware ou par la session. C'est pourtant le total que le
    navigateur paie toutes les dix secondes, et il doit être constant : s'il
    croît avec la flotte, un N+1 s'est glissé quelque part.
    """

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        self.adresse = reverse("fleet:dashboard_kpi")

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
        """Les six requêtes de calculer_kpi, plus celles de l'authentification.

        Deux de plus que la fonction seule : la session et l'utilisateur, que
        le middleware d'authentification charge à chaque requête. Elles ne
        dépendent pas de la flotte, et aucune autre ne s'y ajoute — la vue
        n'interroge la base que par calculer_kpi.
        """
        creer_flotte(5)
        with self.assertNumQueries(8):
            self.client.get(self.adresse)

    def test_la_page_coute_une_requete_de_plus_que_l_endpoint(self):
        """Celle du bandeau d'alertes, posée par le context processor.

        Le JSON ne rend aucun gabarit : les context processors ne tournent
        pas, et cette requête n'est pas payée toutes les dix secondes.
        """
        creer_flotte(5)
        with CaptureQueriesContext(connection) as endpoint:
            self.client.get(self.adresse)
        with CaptureQueriesContext(connection) as page:
            self.client.get(reverse("fleet:dashboard"))
        self.assertEqual(len(page), len(endpoint) + 1)


class LesChiffresViennentDesServicesTest(TestCase):
    """La page n'invente aucun calcul : elle affiche ce que kpi.py a trouvé."""

    @classmethod
    def setUpTestData(cls):
        creer_flotte(4)

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def test_le_contexte_porte_le_dictionnaire_de_calculer_kpi(self):
        reponse = self.client.get(reverse("fleet:dashboard"))
        donnees = reponse.context["donnees"]
        attendu = calculer_kpi()
        for cle in (
            "en_service",
            "en_mouvement",
            "km_du_jour",
            "consommation_moy",
            "echeances_60j",
            "total_camions",
        ):
            with self.subTest(cle=cle):
                self.assertEqual(donnees[cle], attendu[cle])

    def test_la_fenetre_de_consommation_est_celle_des_services(self):
        """Le libellé de la carte cite la constante, il ne la recopie pas."""
        reponse = self.client.get(reverse("fleet:dashboard"))
        self.assertContains(
            reponse,
            f"pondérée, {services.FENETRE_CONSOMMATION_JOURS} derniers jours",
        )
