"""Tests des vues : accès, filtres, CRUD et affichage des refus.

Ces tests rendent réellement les gabarits. Une balise mal fermée, un nom de
route inexistant dans un {% url %} ou une variable mal orthographiée font donc
échouer la suite — ce qu'aucun test de service ne pourrait détecter.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleet import services
from fleet.models import Document, Entretien, Mission, PleinCarburant, Vehicule
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

    def test_la_liste_affiche_le_statut_gps_et_le_fournisseur(self):
        """Les deux colonnes ajoutees en phase B.

        La colonne « Assurance » a quitte la liste pour suivre la maquette :
        l'etat de l'assurance reste sur la fiche du camion et dans la page
        Documents. Ce test a donc remplace celui qui cherchait « Aucune
        attestation » ici.
        """
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertContains(reponse, "Statut GPS")
        self.assertContains(reponse, "Fournisseur")
        # Aucun releve pour ces camions de test : l'etat est « aucune donnee ».
        self.assertContains(reponse, "Aucune donnée")
        self.assertContains(reponse, "non équipé")

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
        self.assertContains(reponse, "ff-badge--rouge")
        self.assertContains(reponse, "Expiré depuis 10 j")

    def test_la_fiche_signale_une_assurance_absente(self):
        """Un dossier d'assurance vide doit alerter, pas rassurer."""
        reponse = self.client.get(self.vehicule.get_absolute_url())
        self.assertContains(reponse, "Aucune attestation")
        self.assertContains(reponse, "ff-badge--ambre")

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
        self.assertContains(reponse, "ff-bandeau--rouge")


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
        self.assertContains(reponse, "ff-bandeau--rouge")

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


class MissionVuesTest(VueConnecteeTest):
    """Le scénario complet de la Definition of Done, par l'interface web."""

    def setUp(self):
        super().setUp()
        self.camion = creer_vehicule(immatriculation="AB 1234 RB", kilometrage=400_000)
        self.utilitaire = creer_vehicule(immatriculation="AC 4821 RB")
        self.rodrigue = creer_chauffeur(nom="Rodrigue Hounkpatin", permis="BJ-1")
        self.aissatou = creer_chauffeur(nom="Aïssatou Zinsou", permis="BJ-2")

    def _donnees(self, **champs):
        valeurs = {
            "vehicule": self.camion.pk,
            "chauffeur": self.rodrigue.pk,
            "depart": "Cotonou",
            "destination": "Parakou",
            "date_depart": self.aujourdhui.isoformat(),
            "km_depart": "",
            "commentaire": "",
        }
        valeurs.update(champs)
        return valeurs

    def test_creation_d_une_mission(self):
        reponse = self.client.post(
            reverse("fleet:mission_creer"), self._donnees(), follow=True
        )
        self.assertContains(reponse, "ouverte avec")

        mission = Mission.objects.get()
        self.assertEqual(mission.statut, Mission.Statut.EN_COURS)
        # km_depart laissé vide : le service reprend le compteur du véhicule.
        self.assertEqual(mission.km_depart, 400_000)

    def test_deuxieme_mission_refusee_proprement(self):
        """Refus affiché en alerte Bootstrap, formulaire ré-affiché rempli."""
        self.client.post(reverse("fleet:mission_creer"), self._donnees())

        reponse = self.client.post(
            reverse("fleet:mission_creer"),
            self._donnees(chauffeur=self.aissatou.pk, destination="Bohicon"),
        )

        # Pas de redirection : on reste sur le formulaire.
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "ff-bandeau--rouge")
        self.assertContains(reponse, "déjà engagé")
        # Les valeurs saisies sont conservées : rien à retaper.
        self.assertContains(reponse, "Bohicon")
        self.assertEqual(Mission.objects.count(), 1)

    def test_cloture_par_l_interface(self):
        self.client.post(reverse("fleet:mission_creer"), self._donnees())
        mission = Mission.objects.get()

        reponse = self.client.post(
            reverse("fleet:mission_cloturer", args=[mission.pk]),
            {
                "date_arrivee": self.aujourdhui.isoformat(),
                "km_arrivee": "400430",
                "commentaire": "Livraison conforme.",
            },
            follow=True,
        )

        self.assertContains(reponse, "430 km parcourus")
        mission.refresh_from_db()
        self.camion.refresh_from_db()
        self.assertEqual(mission.statut, Mission.Statut.TERMINEE)
        self.assertEqual(self.camion.kilometrage, 400_430)
        self.assertEqual(self.camion.statut, Vehicule.Statut.DISPONIBLE)

    def test_cloture_refusee_si_kilometrage_incoherent(self):
        self.client.post(reverse("fleet:mission_creer"), self._donnees())
        mission = Mission.objects.get()

        reponse = self.client.post(
            reverse("fleet:mission_cloturer", args=[mission.pk]),
            {
                "date_arrivee": self.aujourdhui.isoformat(),
                "km_arrivee": "399000",
                "commentaire": "",
            },
        )

        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "ff-bandeau--rouge")
        mission.refresh_from_db()
        self.assertEqual(mission.statut, Mission.Statut.EN_COURS)

    def test_liste_et_filtres(self):
        self.client.post(reverse("fleet:mission_creer"), self._donnees())
        Mission.objects.create(
            vehicule=self.utilitaire,
            chauffeur=self.aissatou,
            depart="Cotonou",
            destination="Lomé",
            date_depart=self.aujourdhui - timedelta(days=40),
            km_depart=1_000,
            km_arrivee=1_150,
            date_arrivee=self.aujourdhui - timedelta(days=39),
            statut=Mission.Statut.TERMINEE,
        )

        reponse = self.client.get(reverse("fleet:mission_liste"))
        self.assertContains(reponse, "Parakou")
        self.assertContains(reponse, "Lomé")

        # Filtre par véhicule.
        reponse = self.client.get(
            reverse("fleet:mission_liste"), {"vehicule": self.camion.pk}
        )
        self.assertContains(reponse, "Parakou")
        self.assertNotContains(reponse, "Lomé")

        # Filtre par statut.
        reponse = self.client.get(
            reverse("fleet:mission_liste"), {"statut": "terminee"}
        )
        self.assertContains(reponse, "Lomé")
        self.assertNotContains(reponse, "Parakou")

        # Filtre par période : la mission ancienne sort de la fenêtre.
        reponse = self.client.get(
            reverse("fleet:mission_liste"),
            {"debut": (self.aujourdhui - timedelta(days=7)).isoformat()},
        )
        self.assertContains(reponse, "Parakou")
        self.assertNotContains(reponse, "Lomé")

    def test_une_date_illisible_dans_l_url_ne_casse_pas_la_page(self):
        """Une adresse bricolée à la main ne doit pas provoquer d'erreur 500."""
        for valeur in ("pas-une-date", "2026-02-31", "31/09/2026"):
            with self.subTest(valeur=valeur):
                reponse = self.client.get(
                    reverse("fleet:mission_liste"), {"debut": valeur}
                )
                self.assertEqual(reponse.status_code, 200)

    def test_seules_les_missions_en_cours_ont_un_bouton_cloturer(self):
        self.client.post(reverse("fleet:mission_creer"), self._donnees())
        mission = Mission.objects.get()
        reponse = self.client.get(reverse("fleet:mission_liste"))
        self.assertContains(
            reponse, reverse("fleet:mission_cloturer", args=[mission.pk])
        )

        services.cloturer_mission(
            mission, date_arrivee=self.aujourdhui, km_arrivee=400_500
        )
        reponse = self.client.get(reverse("fleet:mission_liste"))
        self.assertNotContains(
            reponse, reverse("fleet:mission_cloturer", args=[mission.pk])
        )


class PleinVuesTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule(kilometrage=100_000)
        self.chauffeur = creer_chauffeur()

    def _donnees(self, **champs):
        valeurs = {
            "vehicule": self.vehicule.pk,
            "chauffeur": self.chauffeur.pk,
            "date": self.aujourdhui.isoformat(),
            "litres": "45",
            "prix_litre": "700",
            "km_compteur": "100500",
        }
        valeurs.update(champs)
        return valeurs

    def test_premier_plein_annonce_l_absence_de_consommation(self):
        reponse = self.client.post(
            reverse("fleet:plein_creer"), self._donnees(), follow=True
        )
        self.assertContains(reponse, "premier plein")
        self.assertContains(reponse, "31500 FCFA")

    def test_le_deuxieme_plein_affiche_la_consommation(self):
        self.client.post(
            reverse("fleet:plein_creer"),
            self._donnees(
                date=(self.aujourdhui - timedelta(days=10)).isoformat(),
                km_compteur="100000",
            ),
        )
        reponse = self.client.post(
            reverse("fleet:plein_creer"),
            self._donnees(km_compteur="100500", litres="45"),
            follow=True,
        )
        # 45 L pour 500 km.
        self.assertContains(reponse, "9.00 L/100 km")

    def test_refus_affiche_en_alerte(self):
        reponse = self.client.post(reverse("fleet:plein_creer"), self._donnees(litres="0"))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(PleinCarburant.objects.count(), 0)

    def test_la_liste_affiche_la_consommation(self):
        self.client.post(
            reverse("fleet:plein_creer"),
            self._donnees(
                date=(self.aujourdhui - timedelta(days=10)).isoformat(),
                km_compteur="100000",
            ),
        )
        self.client.post(reverse("fleet:plein_creer"), self._donnees())

        reponse = self.client.get(reverse("fleet:plein_liste"))
        self.assertContains(reponse, "9.00")
        self.assertContains(reponse, "31500 FCFA")

    def test_filtre_par_vehicule(self):
        autre = creer_vehicule(immatriculation="ZZ 9999 RB")
        self.client.post(reverse("fleet:plein_creer"), self._donnees())
        reponse = self.client.get(
            reverse("fleet:plein_liste"), {"vehicule": autre.pk}
        )
        # La plaque du premier véhicule figure encore dans la liste déroulante
        # du filtre : c'est le tableau qu'il faut regarder, pas la page entière.
        self.assertContains(reponse, "Aucun plein ne correspond")


class EntretienVuesTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule(kilometrage=80_000)

    def test_saisie_et_echeance_calculee(self):
        reponse = self.client.post(
            reverse("fleet:entretien_creer"),
            {
                "vehicule": self.vehicule.pk,
                "type_entretien": Entretien.TypeEntretien.VIDANGE,
                "date": self.aujourdhui.isoformat(),
                "km": "80000",
                "cout": "35000",
                "prestataire": "Garage Sodji",
                "prochaine_echeance_km": "",
            },
            follow=True,
        )
        self.assertContains(reponse, "Prochaine échéance à 85000 km")

    def test_badge_en_retard(self):
        """Le compteur a dépassé l'échéance : l'entretien est en retard."""
        Entretien.objects.create(
            vehicule=self.vehicule,
            type_entretien=Entretien.TypeEntretien.VIDANGE,
            date=self.aujourdhui - timedelta(days=60),
            km=74_000,
            cout=30_000,
            prestataire="Garage Tokpa Auto",
            prochaine_echeance_km=79_000,
        )
        reponse = self.client.get(reverse("fleet:entretien_liste"))
        self.assertContains(reponse, "En retard de 1000 km")
        self.assertContains(reponse, "ff-badge--rouge")

    def test_badge_a_venir(self):
        Entretien.objects.create(
            vehicule=self.vehicule,
            type_entretien=Entretien.TypeEntretien.PNEUS,
            date=self.aujourdhui - timedelta(days=10),
            km=79_000,
            cout=200_000,
            prestataire="Pneus Service Cotonou",
            prochaine_echeance_km=119_000,
        )
        reponse = self.client.get(reverse("fleet:entretien_liste"))
        self.assertContains(reponse, "À venir dans 39000 km")


class DocumentVuesTest(VueConnecteeTest):
    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule()
        self.expire = creer_document(
            self.vehicule, Document.TypeDocument.VISITE_TECHNIQUE, jours=-15
        )
        self.valide = creer_document(
            self.vehicule, Document.TypeDocument.ASSURANCE, jours=300
        )
        self.permanent = creer_document(
            self.vehicule, Document.TypeDocument.CARTE_GRISE, date_expiration=None
        )

    def test_liste_avec_badges(self):
        reponse = self.client.get(reverse("fleet:document_liste"))
        self.assertContains(reponse, "Expiré depuis 15 j")
        self.assertContains(reponse, "ff-badge--rouge")
        self.assertContains(reponse, "ff-badge--vert")
        self.assertContains(reponse, "Sans échéance")

    def test_filtre_sur_les_alertes(self):
        reponse = self.client.get(reverse("fleet:document_liste"), {"etat": "alerte"})
        self.assertContains(reponse, "Expiré depuis 15 j")
        # On teste l'absence du badge et non du libellé « Carte grise » : ce
        # dernier figure aussi dans la liste déroulante du filtre par type,
        # qui est affichée quels que soient les résultats.
        self.assertNotContains(reponse, "Sans échéance")
        self.assertNotContains(reponse, "ff-badge--vert")

    def test_filtre_par_type(self):
        reponse = self.client.get(
            reverse("fleet:document_liste"),
            {"type": Document.TypeDocument.ASSURANCE},
        )
        self.assertContains(reponse, "ff-badge--vert")
        self.assertNotContains(reponse, "Sans échéance")
        self.assertNotContains(reponse, "Expiré depuis 15 j")


class FicheVehiculeActiviteTest(VueConnecteeTest):
    """La fiche véhicule rassemble missions, pleins, entretiens et documents."""

    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule(kilometrage=50_000)
        self.chauffeur = creer_chauffeur()

    def test_la_fiche_montre_les_trois_historiques(self):
        mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Bohicon",
            date_depart=self.aujourdhui - timedelta(days=2),
        )
        services.cloturer_mission(
            mission, date_arrivee=self.aujourdhui - timedelta(days=1), km_arrivee=50_130
        )
        services.enregistrer_plein(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui,
            litres=Decimal("40"),
            prix_litre=Decimal("715"),
            km_compteur=50_200,
        )
        services.enregistrer_entretien(
            vehicule=self.vehicule,
            type_entretien=Entretien.TypeEntretien.VIDANGE,
            date=self.aujourdhui,
            km=50_200,
            cout=Decimal("32000"),
            prestataire="Garage Sodji",
        )

        reponse = self.client.get(self.vehicule.get_absolute_url())

        self.assertContains(reponse, "10 dernières missions")
        self.assertContains(reponse, "Bohicon")
        self.assertContains(reponse, "10 derniers pleins")
        # intcomma sépare les milliers par une espace insécable (U+00A0) en
        # locale fr-fr : la page affiche « 28 600 » et non « 28600 ».
        self.assertContains(reponse, "28\u00a0600")
        self.assertContains(reponse, "Vidange")
        self.assertContains(reponse, "À venir dans 5000 km")

    def test_la_fiche_ne_multiplie_pas_les_requetes(self):
        """Garde-fou contre le N+1 : afficher plus de lignes ne coûte pas plus.

        Figer un nombre exact de requêtes rendrait ce test cassant : la moindre
        évolution de la page le ferait échouer sans qu'aucun bug n'existe. On
        compare donc deux mesures — une fiche avec une mission, la même avec
        six. Si le compte augmente, c'est qu'un select_related manque et que la
        page interroge la base une fois par ligne affichée.
        """

        def creer_missions(nombre, decalage):
            for index in range(nombre):
                jour = decalage + index + 1
                Mission.objects.create(
                    vehicule=self.vehicule,
                    chauffeur=self.chauffeur,
                    depart="Cotonou",
                    destination=f"Ville {jour}",
                    date_depart=self.aujourdhui - timedelta(days=jour),
                    km_depart=40_000 + jour * 100,
                    km_arrivee=40_050 + jour * 100,
                    date_arrivee=self.aujourdhui - timedelta(days=jour),
                    statut=Mission.Statut.TERMINEE,
                )

        creer_missions(1, 0)
        with CaptureQueriesContext(connection) as avec_une_mission:
            self.client.get(self.vehicule.get_absolute_url())

        creer_missions(5, 1)
        with CaptureQueriesContext(connection) as avec_six_missions:
            self.client.get(self.vehicule.get_absolute_url())

        self.assertEqual(len(avec_une_mission), len(avec_six_missions))


class FicheChauffeurHistoriqueTest(VueConnecteeTest):
    def test_historique_des_missions(self):
        vehicule = creer_vehicule()
        chauffeur = creer_chauffeur()
        Mission.objects.create(
            vehicule=vehicule,
            chauffeur=chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui - timedelta(days=5),
            km_depart=1_000,
            km_arrivee=1_410,
            date_arrivee=self.aujourdhui - timedelta(days=4),
            statut=Mission.Statut.TERMINEE,
        )
        reponse = self.client.get(chauffeur.get_absolute_url())
        self.assertContains(reponse, "Historique des missions")
        self.assertContains(reponse, "410 km")


class AffichageDesFormulairesTest(VueConnecteeTest):
    """Charger en GET chaque écran de saisie.

    Ces tests existent à cause d'un bug réel : le gabarit de connexion avait un
    {% comment %} avant son {% extends %}, ce que Django refuse, et personne ne
    l'a vu parce que les tests ne faisaient que des POST. Un formulaire qu'on
    n'affiche jamais dans les tests est un formulaire non testé, même si son
    POST fonctionne : le rendu du gabarit est un chemin de code à part entière.
    """

    def setUp(self):
        super().setUp()
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def test_les_ecrans_de_saisie_s_affichent(self):
        adresses = [
            reverse("fleet:vehicule_creer"),
            reverse("fleet:vehicule_modifier", args=[self.vehicule.pk]),
            reverse("fleet:chauffeur_creer"),
            reverse("fleet:chauffeur_modifier", args=[self.chauffeur.pk]),
            reverse("fleet:mission_creer"),
            reverse("fleet:plein_creer"),
            reverse("fleet:entretien_creer"),
        ]
        for adresse in adresses:
            with self.subTest(adresse=adresse):
                reponse = self.client.get(adresse)
                self.assertEqual(reponse.status_code, 200)
                self.assertContains(reponse, "Enregistrer")

    def test_l_ecran_de_cloture_affiche_la_mission(self):
        mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        reponse = self.client.get(
            reverse("fleet:mission_cloturer", args=[mission.pk])
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "Mission à clôturer")
        self.assertContains(reponse, "Parakou")
        # Le relevé de départ est rappelé, pour éviter une saisie incohérente.
        self.assertContains(reponse, str(mission.km_depart))

    def test_les_listes_s_affichent_meme_vides(self):
        """Une base sans données ne doit pas casser une page."""
        Mission.objects.all().delete()
        for nom in [
            "fleet:mission_liste",
            "fleet:plein_liste",
            "fleet:entretien_liste",
            "fleet:document_liste",
        ]:
            with self.subTest(vue=nom):
                reponse = self.client.get(reverse(nom))
                self.assertEqual(reponse.status_code, 200)
                self.assertContains(reponse, "Aucun")

    def test_une_mission_deja_cloturee_reste_affichable(self):
        """L'écran de clôture s'ouvre, le service refusera l'enregistrement."""
        mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Bohicon",
            date_depart=self.aujourdhui,
        )
        services.cloturer_mission(
            mission, date_arrivee=self.aujourdhui, km_arrivee=100_200
        )

        reponse = self.client.get(
            reverse("fleet:mission_cloturer", args=[mission.pk])
        )
        self.assertEqual(reponse.status_code, 200)

        reponse = self.client.post(
            reverse("fleet:mission_cloturer", args=[mission.pk]),
            {
                "date_arrivee": self.aujourdhui.isoformat(),
                "km_arrivee": "100500",
                "commentaire": "",
            },
        )
        self.assertContains(reponse, "ff-bandeau--rouge")
        self.assertContains(reponse, "en cours")
