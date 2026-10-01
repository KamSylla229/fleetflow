"""Tests du thème : ossature des pages, menu actif et ressources locales.

Ces tests ne vérifient pas que l'application est *belle* — aucun test ne sait
faire ça. Ils vérifient ce qui se casse en silence : une page qui perd sa
barre de navigation, un mauvais onglet marqué actif, et surtout une ressource
qui repart vers un CDN, ce qui ne se voit qu'une fois la connexion coupée.
"""

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from fleet import services
from fleet.context_processors import initiales
from fleet.models import Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule


class OssatureDesPagesTest(TestCase):
    """Chaque page répond 200, porte la barre de navigation et le bon onglet."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def pages(self):
        """(adresse, onglet attendu) pour toutes les pages de l'application."""
        return [
            (reverse("fleet:vehicule_liste"), "camions"),
            (reverse("fleet:vehicule_creer"), "camions"),
            (reverse("fleet:vehicule_detail", args=[self.vehicule.pk]), "camions"),
            (reverse("fleet:vehicule_modifier", args=[self.vehicule.pk]), "camions"),
            (reverse("fleet:chauffeur_liste"), "chauffeurs"),
            (reverse("fleet:chauffeur_creer"), "chauffeurs"),
            (reverse("fleet:chauffeur_detail", args=[self.chauffeur.pk]), "chauffeurs"),
            (reverse("fleet:chauffeur_modifier", args=[self.chauffeur.pk]), "chauffeurs"),
            (reverse("fleet:mission_liste"), "missions"),
            (reverse("fleet:mission_creer"), "missions"),
            (reverse("fleet:plein_liste"), "carburant"),
            (reverse("fleet:plein_creer"), "carburant"),
            (reverse("fleet:entretien_liste"), "entretien"),
            (reverse("fleet:entretien_creer"), "entretien"),
            (reverse("fleet:document_liste"), "documents"),
            (reverse("fleet:alerte_liste"), "alertes"),
        ]

    def test_chaque_page_repond_et_porte_la_navigation(self):
        for adresse, onglet in self.pages():
            with self.subTest(adresse=adresse):
                reponse = self.client.get(adresse)
                self.assertEqual(reponse.status_code, 200)
                self.assertContains(reponse, "ff-sidebar")
                self.assertContains(reponse, "ff-nav__item--actif")

    def test_le_bon_onglet_est_marque_actif(self):
        """nav_actif vient du context processor, pas des vues."""
        for adresse, onglet in self.pages():
            with self.subTest(adresse=adresse):
                reponse = self.client.get(adresse)
                self.assertEqual(reponse.context["nav_actif"], onglet)

    def test_les_entrees_sans_page_ne_sont_pas_cliquables(self):
        """Tableau de bord, Carte, Rapports et Fournisseurs GPS sont annoncés.

        Ce sont des <span> et non des liens : une entrée désactivée reste
        atteignable au clavier et promet une destination qui n'existe pas.
        """
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        for libelle in ["Tableau de bord", "Carte", "Rapports", "Fournisseurs GPS"]:
            with self.subTest(libelle=libelle):
                self.assertContains(reponse, libelle)
        self.assertContains(reponse, "ff-nav__item--bientot")
        self.assertContains(reponse, "bientôt")

    def test_la_page_de_connexion_n_a_pas_de_navigation(self):
        """Elle hérite de base_public.html : ni sidebar, ni menu."""
        self.client.logout()
        reponse = self.client.get(reverse("login"))
        self.assertEqual(reponse.status_code, 200)
        self.assertNotContains(reponse, "ff-sidebar")
        self.assertContains(reponse, "ff-connexion")
        self.assertContains(reponse, "Se connecter")

    def test_le_titre_de_page_est_rempli_partout(self):
        """Un <title> vide est le signe d'un bloc oublié."""
        for adresse, _onglet in self.pages():
            with self.subTest(adresse=adresse):
                reponse = self.client.get(adresse)
                contenu = reponse.content.decode("utf-8")
                debut = contenu.index("<title>") + len("<title>")
                titre = contenu[debut : contenu.index("</title>")].strip()
                self.assertNotEqual(titre, "")
                self.assertIn("FleetFlow", titre)


class RessourcesLocalesTest(TestCase):
    """Le garde-fou de la Definition of Done : « Wi-Fi coupé, le style reste ».

    Une référence à un CDN ne casse rien tant qu'on est connecté : c'est
    exactement le genre de régression qu'aucun test d'affichage ne voit et
    qu'on découvre chez le client.
    """

    HOTES_INTERDITS = (
        "cdn.jsdelivr.net",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdnjs.cloudflare.com",
        "unpkg.com",
    )

    FICHIERS_ATTENDUS = (
        "css/fleetflow.css",
        "css/polices.css",
        "vendor/bootstrap/bootstrap.min.css",
        "vendor/bootstrap/bootstrap.bundle.min.js",
        "vendor/icones/bootstrap-icons.css",
        "vendor/icones/fonts/bootstrap-icons.woff2",
        "js/compteur-alerte.js",
    )

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        creer_vehicule()

    def test_aucune_page_ne_pointe_vers_un_cdn(self):
        adresses = [
            reverse("fleet:vehicule_liste"),
            reverse("fleet:chauffeur_liste"),
            reverse("fleet:mission_liste"),
            reverse("fleet:plein_liste"),
            reverse("fleet:entretien_liste"),
            reverse("fleet:document_liste"),
            reverse("login"),
        ]
        for adresse in adresses:
            reponse = self.client.get(adresse)
            contenu = reponse.content.decode("utf-8")
            for hote in self.HOTES_INTERDITS:
                with self.subTest(adresse=adresse, hote=hote):
                    self.assertNotIn(hote, contenu)

    def test_les_pages_chargent_bien_le_theme(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertContains(reponse, "css/fleetflow.css")
        self.assertContains(reponse, "vendor/bootstrap/bootstrap.min.css")

    def test_les_fichiers_existent_sur_le_disque(self):
        """Une balise <link> vers un fichier absent ne lève aucune erreur.

        finders.find() parcourt STATICFILES_DIRS comme le ferait le serveur :
        si un fichier manque, la page s'affichera sans style sans rien dire.
        """
        for chemin in self.FICHIERS_ATTENDUS:
            with self.subTest(chemin=chemin):
                self.assertIsNotNone(
                    finders.find(chemin), f"{chemin} est introuvable dans static/"
                )


class BadgeTest(TestCase):
    """Le gabarit partials/_badge.html, seul traducteur ton -> classe."""

    def test_un_statut_de_service_donne_la_bonne_classe(self):
        vehicule = creer_vehicule(statut=Vehicule.Statut.EN_MAINTENANCE)
        rendu = render_to_string(
            "partials/_badge.html",
            {"statut": services.statut_operationnel(vehicule)},
        )
        self.assertIn("ff-badge--ambre", rendu)
        self.assertIn("En maintenance", rendu)

    def test_appel_direct_par_ton_et_libelle(self):
        rendu = render_to_string(
            "partials/_badge.html", {"ton": "vert", "libelle": "En route"}
        )
        self.assertIn("ff-badge--vert", rendu)
        self.assertIn("En route", rendu)

    def test_ton_par_defaut_neutre(self):
        """Sans ton fourni, la pastille reste neutre plutôt que sans classe."""
        rendu = render_to_string("partials/_badge.html", {"libelle": "Inconnu"})
        self.assertIn("ff-badge--neutre", rendu)


class InitialesTest(TestCase):
    """Les initiales de la pastille utilisateur, calculées hors du gabarit."""

    def test_prenom_et_nom(self):
        utilisateur = get_user_model().objects.create_user(
            username="ksylla", first_name="Kamel", last_name="Sylla"
        )
        self.assertEqual(initiales(utilisateur), "KS")

    def test_identifiant_seul(self):
        utilisateur = get_user_model().objects.create_user(username="demo")
        self.assertEqual(initiales(utilisateur), "DE")

    def test_visiteur_anonyme(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(initiales(AnonymousUser()), "")

    def test_aucun_utilisateur(self):
        self.assertEqual(initiales(None), "")


class PaginationTest(TestCase):
    """Toutes les listes doivent supporter d'avoir plus d'une page.

    Ce test existe à cause d'une erreur 500 signalée en séance sur /missions/
    et /documents/. Le gabarit de pagination appelait
    `page_obj.previous_page_number` sur la première page, où cette méthode
    lève une EmptyPage au lieu de renvoyer None.

    Aucun test ne l'avait vu, et la raison est instructive : tous les jeux de
    données de test tiennent sur une seule page, or le bloc de pagination
    n'est rendu qu'à partir de deux. **Un composant qui ne s'affiche que dans
    un cas limite doit être testé dans ce cas limite**, pas dans le cas
    ordinaire.

    On crée donc volontairement plus d'objets que la taille d'une page (25).
    """

    PAR_PAGE = 26

    @classmethod
    def setUpTestData(cls):
        """Les données sont créées une fois pour toute la classe.

        setUpTestData et non setUp : créer cent trente objets à chaque test
        serait trois fois plus lent pour un jeu de données que personne ne
        modifie.
        """
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from fleet.models import Document, Entretien, Mission, PleinCarburant

        aujourdhui = timezone.localdate()

        cls.vehicules = [
            creer_vehicule(immatriculation=f"PA {1000 + index} RB")
            for index in range(cls.PAR_PAGE)
        ]
        cls.chauffeurs = [
            creer_chauffeur(nom=f"Chauffeur {index}", permis=f"BJ-PA-{index}")
            for index in range(cls.PAR_PAGE)
        ]
        vehicule = cls.vehicules[0]
        chauffeur = cls.chauffeurs[0]

        Mission.objects.bulk_create(
            Mission(
                vehicule=vehicule,
                chauffeur=chauffeur,
                depart="Cotonou",
                destination=f"Ville {index}",
                date_depart=aujourdhui - timedelta(days=index + 1),
                date_arrivee=aujourdhui - timedelta(days=index + 1),
                km_depart=1_000 + index * 100,
                km_arrivee=1_050 + index * 100,
                statut=Mission.Statut.TERMINEE,
            )
            for index in range(cls.PAR_PAGE)
        )
        Document.objects.bulk_create(
            Document(
                vehicule=vehicule,
                type_document=Document.TypeDocument.AUTRE,
                numero=f"DOC-{index}",
                date_expiration=aujourdhui + timedelta(days=index + 1),
            )
            for index in range(cls.PAR_PAGE)
        )
        PleinCarburant.objects.bulk_create(
            PleinCarburant(
                vehicule=vehicule,
                chauffeur=chauffeur,
                date=aujourdhui - timedelta(days=index + 1),
                litres=Decimal("40"),
                prix_litre=Decimal("715"),
                km_compteur=10_000 + index * 500,
            )
            for index in range(cls.PAR_PAGE)
        )
        Entretien.objects.bulk_create(
            Entretien(
                vehicule=vehicule,
                type_entretien=Entretien.TypeEntretien.VIDANGE,
                date=aujourdhui - timedelta(days=index + 1),
                km=5_000 + index * 100,
                cout=Decimal("30000"),
                prestataire="Garage Akpakpa",
            )
            for index in range(cls.PAR_PAGE)
        )

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)

    def listes(self):
        return [
            "fleet:vehicule_liste",
            "fleet:chauffeur_liste",
            "fleet:mission_liste",
            "fleet:plein_liste",
            "fleet:entretien_liste",
            "fleet:document_liste",
        ]

    def test_la_premiere_page_s_affiche(self):
        """C'est précisément le cas qui levait une EmptyPage."""
        for nom in self.listes():
            with self.subTest(vue=nom):
                reponse = self.client.get(reverse(nom))
                self.assertEqual(reponse.status_code, 200)
                self.assertContains(reponse, "ff-pagination")
                self.assertGreater(reponse.context["page_obj"].paginator.num_pages, 1)

    def test_la_deuxieme_page_s_affiche(self):
        for nom in self.listes():
            with self.subTest(vue=nom):
                reponse = self.client.get(reverse(nom), {"page": "2"})
                self.assertEqual(reponse.status_code, 200)
                self.assertEqual(reponse.context["page_obj"].number, 2)

    def test_les_boutons_sont_desactives_aux_extremites(self):
        """Pas de lien mort : un <span> grisé, et aucun href à suivre."""
        reponse = self.client.get(reverse("fleet:document_liste"))
        self.assertContains(reponse, '<span class="page-link">Précédent</span>')

        derniere = reponse.context["page_obj"].paginator.num_pages
        reponse = self.client.get(
            reverse("fleet:document_liste"), {"page": str(derniere)}
        )
        self.assertContains(reponse, '<span class="page-link">Suivant</span>')

    def test_les_filtres_survivent_au_changement_de_page(self):
        reponse = self.client.get(
            reverse("fleet:document_liste"), {"type": "autre", "page": "2"}
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.context["parametres_filtres"], "type=autre")
        # Le lien « Précédent » doit reconduire le filtre.
        self.assertContains(reponse, "page=1&amp;type=autre")

    def test_un_numero_de_page_absurde_renvoie_404(self):
        """Et non une erreur 500 : une adresse bricolée n'est pas un bug."""
        for page in ("0", "999", "abc", "-1"):
            with self.subTest(page=page):
                reponse = self.client.get(
                    reverse("fleet:document_liste"), {"page": page}
                )
                self.assertEqual(reponse.status_code, 404)
