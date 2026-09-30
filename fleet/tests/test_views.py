"""Tests des vues : accès, filtres, CRUD et affichage des refus.

Ces tests rendent réellement les gabarits. Une balise mal fermée, un nom de
route inexistant dans un {% url %} ou une variable mal orthographiée font donc
échouer la suite — ce qu'aucun test de service ne pourrait détecter.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleet import services
from fleet.models import Document, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule

MOT_DE_PASSE = "motdepasse-de-test"


class AccesTest(TestCase):
    """Aucune page de FleetFlow ne doit s'ouvrir sans connexion."""

    def setUp(self):
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def test_toutes_les_vues_exigent_une_connexion(self):
        adresses = [
            reverse("fleet:accueil"),
            reverse("fleet:vehicule_liste"),
            reverse("fleet:vehicule_creer"),
            reverse("fleet:vehicule_detail", args=[self.vehicule.pk]),
            reverse("fleet:vehicule_modifier", args=[self.vehicule.pk]),
            reverse("fleet:chauffeur_liste"),
            reverse("fleet:chauffeur_creer"),
            reverse("fleet:chauffeur_detail", args=[self.chauffeur.pk]),
            reverse("fleet:chauffeur_modifier", args=[self.chauffeur.pk]),
        ]
        for adresse in adresses:
            # subTest nomme chaque cas : si une seule adresse est ouverte à
            # tous, le rapport d'échec dit laquelle au lieu de s'arrêter à la
            # première.
            with self.subTest(adresse=adresse):
                reponse = self.client.get(adresse)
                self.assertEqual(reponse.status_code, 302)
                self.assertIn(reverse("login"), reponse.url)

    def test_les_actions_d_ecriture_aussi(self):
        for adresse in [
            reverse("fleet:vehicule_activation", args=[self.vehicule.pk]),
            reverse("fleet:chauffeur_activation", args=[self.chauffeur.pk]),
        ]:
            with self.subTest(adresse=adresse):
                reponse = self.client.post(adresse)
                self.assertEqual(reponse.status_code, 302)
                self.assertIn(reverse("login"), reponse.url)

    def test_l_activation_refuse_la_methode_get(self):
        """Une adresse qui modifie des données ne répond pas à un GET."""
        # force_login ouvre la session sans verifier de mot de passe : ne pas
        # en definir evite un hachage PBKDF2 inutile, qui couterait pres d'une
        # seconde a chaque test.
        utilisateur = get_user_model().objects.create_user(username="gestionnaire")
        self.client.force_login(utilisateur)
        reponse = self.client.get(
            reverse("fleet:vehicule_activation", args=[self.vehicule.pk])
        )
        self.assertEqual(reponse.status_code, 405)

    def test_la_page_de_connexion_s_affiche(self):
        """Rendre reellement la page de connexion.

        Ce test manquait, et son absence a laisse passer un bug : le gabarit
        commencait par un {% comment %} place avant {% extends %}, ce que
        Django refuse. Les autres tests ne verifiaient que la redirection vers
        /login/ ou le POST de connexion, jamais l'affichage du formulaire.
        """
        reponse = self.client.get(reverse("login"))
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "Se connecter")

    def test_connexion_puis_redirection(self):
        get_user_model().objects.create_user(
            username="gestionnaire", password=MOT_DE_PASSE
        )
        reponse = self.client.post(
            reverse("login"),
            {"username": "gestionnaire", "password": MOT_DE_PASSE},
            follow=True,
        )
        # LOGIN_REDIRECT_URL mène à la liste des véhicules.
        self.assertRedirects(reponse, reverse("fleet:vehicule_liste"))


class VueConnecteeTest(TestCase):
    """Socle des tests qui ont besoin d'un utilisateur connecté."""

    def setUp(self):
        # Pas de mot de passe : voir le commentaire de AccesTest.
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        self.aujourdhui = timezone.localdate()


class VehiculeListeTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.actros = creer_vehicule(
            immatriculation="AB 1234 RB", marque="Mercedes-Benz", modele="Actros"
        )
        self.hiace = creer_vehicule(
            immatriculation="AC 4821 RB",
            marque="Toyota",
            modele="Hiace",
            statut=Vehicule.Statut.EN_MAINTENANCE,
        )
        self.sorti = creer_vehicule(immatriculation="AD 0000 RB", actif=False)

    def test_la_liste_affiche_la_flotte_active(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "AB 1234 RB")
        # Un véhicule sorti de la flotte n'encombre pas l'écran de travail.
        self.assertNotContains(reponse, "AD 0000 RB")

    def test_filtre_des_vehicules_sortis(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"), {"actif": "0"})
        self.assertContains(reponse, "AD 0000 RB")
        self.assertNotContains(reponse, "AB 1234 RB")

    def test_recherche_sur_la_marque(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"), {"q": "toyota"})
        self.assertContains(reponse, "AC 4821 RB")
        self.assertNotContains(reponse, "AB 1234 RB")

    def test_filtre_par_statut(self):
        reponse = self.client.get(
            reverse("fleet:vehicule_liste"), {"statut": "en_maintenance"}
        )
        self.assertContains(reponse, "AC 4821 RB")
        self.assertNotContains(reponse, "AB 1234 RB")

    def test_le_badge_signale_une_assurance_absente(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertContains(reponse, "Aucune attestation")

    def test_les_filtres_survivent_a_la_pagination(self):
        """parametres_filtres doit contenir la recherche, sans le numéro de page."""
        reponse = self.client.get(
            reverse("fleet:vehicule_liste"), {"q": "toyota", "page": "1"}
        )
        self.assertEqual(reponse.context["parametres_filtres"], "q=toyota")


class VehiculeFicheTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule(kilometrage=200_000)

    def test_un_document_expire_apparait_en_rouge(self):
        """Exigence de la Definition of Done, vérifiée sur le HTML rendu."""
        creer_document(
            self.vehicule, Document.TypeDocument.VISITE_TECHNIQUE, jours=-10
        )
        reponse = self.client.get(self.vehicule.get_absolute_url())
        self.assertContains(reponse, "bg-danger")
        self.assertContains(reponse, "Expiré depuis 10 j")

    def test_une_piece_sans_echeance_reste_neutre(self):
        creer_document(
            self.vehicule,
            Document.TypeDocument.CARTE_GRISE,
            date_expiration=None,
        )
        reponse = self.client.get(self.vehicule.get_absolute_url())
        self.assertContains(reponse, "Sans échéance")

    def test_la_fiche_montre_le_chauffeur_affecte(self):
        chauffeur = creer_chauffeur(nom="Aïssatou Zinsou", permis="BJ-0042")
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        reponse = self.client.get(self.vehicule.get_absolute_url())
        self.assertContains(reponse, "Aïssatou Zinsou")


class VehiculeEcritureTest(VueConnecteeTest):
    def test_creation(self):
        reponse = self.client.post(
            reverse("fleet:vehicule_creer"),
            {
                "immatriculation": "AZ 9999 RB",
                "marque": "Hyundai",
                "modele": "HD78",
                "annee": 2021,
                "type_vehicule": Vehicule.TypeVehicule.CAMION,
                "kilometrage": 15_000,
                "statut": Vehicule.Statut.DISPONIBLE,
            },
            follow=True,
        )
        vehicule = Vehicule.objects.get(immatriculation="AZ 9999 RB")
        self.assertRedirects(reponse, vehicule.get_absolute_url())
        self.assertContains(reponse, "a été enregistré")

    def test_creation_refusee_sur_plaque_en_doublon(self):
        creer_vehicule(immatriculation="AZ 9999 RB")
        reponse = self.client.post(
            reverse("fleet:vehicule_creer"),
            {
                "immatriculation": "AZ 9999 RB",
                "marque": "Hyundai",
                "modele": "HD78",
                "annee": 2021,
                "type_vehicule": Vehicule.TypeVehicule.CAMION,
                "kilometrage": 15_000,
                "statut": Vehicule.Statut.DISPONIBLE,
            },
        )
        # Le formulaire est ré-affiché avec son erreur, et rien n'est créé.
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(Vehicule.objects.count(), 1)

    def test_modification(self):
        vehicule = creer_vehicule(marque="Toyoat")
        self.client.post(
            reverse("fleet:vehicule_modifier", args=[vehicule.pk]),
            {
                "immatriculation": vehicule.immatriculation,
                "marque": "Toyota",
                "modele": vehicule.modele,
                "annee": vehicule.annee,
                "type_vehicule": vehicule.type_vehicule,
                "kilometrage": vehicule.kilometrage,
                "statut": vehicule.statut,
            },
        )
        vehicule.refresh_from_db()
        self.assertEqual(vehicule.marque, "Toyota")

    def test_le_formulaire_ne_permet_pas_de_desactiver(self):
        """`actif` est hors du formulaire : la sortie de flotte a son bouton."""
        reponse = self.client.get(reverse("fleet:vehicule_creer"))
        self.assertNotIn("actif", reponse.context["form"].fields)

    def test_sortie_de_flotte(self):
        vehicule = creer_vehicule()
        reponse = self.client.post(
            reverse("fleet:vehicule_activation", args=[vehicule.pk]), follow=True
        )
        vehicule.refresh_from_db()
        self.assertFalse(vehicule.actif)
        self.assertEqual(vehicule.statut, Vehicule.Statut.HORS_SERVICE)
        self.assertContains(reponse, "sorti de la flotte active")

    def test_reintegration(self):
        vehicule = creer_vehicule(actif=False, statut=Vehicule.Statut.HORS_SERVICE)
        self.client.post(
            reverse("fleet:vehicule_activation", args=[vehicule.pk]), follow=True
        )
        vehicule.refresh_from_db()
        self.assertTrue(vehicule.actif)
        self.assertEqual(vehicule.statut, Vehicule.Statut.DISPONIBLE)

    def test_le_refus_du_service_devient_un_message(self):
        """Un véhicule en mission ne sort pas de la flotte, et l'utilisateur le lit."""
        vehicule = creer_vehicule()
        chauffeur = creer_chauffeur()
        services.creer_mission(
            vehicule=vehicule,
            chauffeur=chauffeur,
            depart="Cotonou",
            destination="Bohicon",
            date_depart=self.aujourdhui,
        )

        reponse = self.client.post(
            reverse("fleet:vehicule_activation", args=[vehicule.pk]), follow=True
        )

        vehicule.refresh_from_db()
        self.assertTrue(vehicule.actif)
        self.assertContains(reponse, "est en mission")
        self.assertContains(reponse, "alert-danger")


class ChauffeurVuesTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.rodrigue = creer_chauffeur(nom="Rodrigue Hounkpatin", permis="BJ-1")
        self.perime = creer_chauffeur(
            nom="Permis Perime",
            permis="BJ-2",
            date_expiration_permis=self.aujourdhui - timedelta(days=4),
        )

    def test_liste(self):
        reponse = self.client.get(reverse("fleet:chauffeur_liste"))
        self.assertContains(reponse, "Rodrigue Hounkpatin")
        self.assertContains(reponse, "Expiré depuis 4 j")

    def test_filtre_permis_expire(self):
        reponse = self.client.get(
            reverse("fleet:chauffeur_liste"), {"permis": services.ECHEANCE_EXPIREE}
        )
        self.assertContains(reponse, "Permis Perime")
        self.assertNotContains(reponse, "Rodrigue Hounkpatin")

    def test_recherche(self):
        reponse = self.client.get(reverse("fleet:chauffeur_liste"), {"q": "BJ-1"})
        self.assertContains(reponse, "Rodrigue Hounkpatin")
        self.assertNotContains(reponse, "Permis Perime")

    def test_fiche_avertit_du_permis_expire(self):
        reponse = self.client.get(self.perime.get_absolute_url())
        self.assertContains(reponse, "Toute nouvelle")
        self.assertContains(reponse, "alert-danger")

    def test_creation(self):
        reponse = self.client.post(
            reverse("fleet:chauffeur_creer"),
            {
                "nom": "Mireille Tchibozo",
                "telephone": "+229 01 66 18 93 25",
                "numero_permis": "BJ-2021-023476",
                "date_expiration_permis": (
                    self.aujourdhui + timedelta(days=500)
                ).isoformat(),
            },
            follow=True,
        )
        self.assertContains(reponse, "a été enregistré")

    def test_desactivation(self):
        self.client.post(
            reverse("fleet:chauffeur_activation", args=[self.rodrigue.pk]), follow=True
        )
        self.rodrigue.refresh_from_db()
        self.assertFalse(self.rodrigue.actif)
