"""Tests des alertes de boîtier muet et de leur notification.

Un piège à connaître avant de lire ces tests : dans un `TestCase`, chaque test
tourne dans une transaction annulée à la fin. Or les notifications sont
programmées par `transaction.on_commit()`, qui n'est **jamais** appelé si la
transaction n'est pas validée. Sans précaution, tous les tests d'e-mail
passeraient avec une boîte d'envoi vide — c'est-à-dire sans rien vérifier.

D'où `self.captureOnCommitCallbacks(execute=True)` : il exécute les fonctions
en attente à la sortie du bloc. C'est l'outil que Django fournit exactement
pour cela.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleet import services
from fleet.models import Alerte, FournisseurGPS, PositionGPS
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule

GERANT = "gerant@fleetflow.invalid"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FLEETFLOW_EMAIL_GERANT=GERANT,
    FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=30,
)
class VerifierSignauxTest(TestCase):
    def setUp(self):
        self.maintenant = timezone.now()
        self.fournisseur = FournisseurGPS.objects.create(nom="Cartrack")
        self.vehicule = creer_vehicule(
            immatriculation="GH 3456 RB",
            fournisseur_gps=self.fournisseur,
            boitier_id="CT-99001",
            itineraire="cotonou_parakou",
        )

    def _position(self, minutes, vitesse="0"):
        return PositionGPS.objects.create(
            vehicule=self.vehicule,
            latitude=Decimal("6.370300"),
            longitude=Decimal("2.391200"),
            vitesse_kmh=Decimal(vitesse),
            horodatage=self.maintenant - timedelta(minutes=minutes),
        )

    def _verifier(self):
        """Appelle verifier_signaux en laissant partir les notifications."""
        with self.captureOnCommitCallbacks(execute=True):
            return services.verifier_signaux(maintenant=self.maintenant)

    # --- Ouverture -----------------------------------------------------------

    def test_une_alerte_est_ouverte_et_le_gerant_prevenu(self):
        self._position(minutes=220)

        bilan = self._verifier()

        self.assertEqual(len(bilan["ouvertes"]), 1)
        alerte = Alerte.objects.get()
        self.assertEqual(alerte.type_alerte, Alerte.TypeAlerte.SANS_SIGNAL)
        self.assertEqual(alerte.vehicule, self.vehicule)
        self.assertTrue(alerte.est_ouverte)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("GH 3456 RB", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [GERANT])
        self.assertIn("CT-99001", mail.outbox[0].body)

        alerte.refresh_from_db()
        self.assertIsNotNone(alerte.email_envoye_le)

    def test_l_alerte_est_datee_du_dernier_releve(self):
        """Le problème commence quand le boîtier se tait, pas quand on le voit.

        Sans ce recalage, le bandeau annoncerait « depuis 2 minutes » pour un
        camion silencieux depuis plus de trois heures.
        """
        position = self._position(minutes=220)
        self._verifier()

        alerte = Alerte.objects.get()
        self.assertEqual(alerte.ouverte_le, position.horodatage)

    def test_une_seule_alerte_malgre_plusieurs_passages(self):
        """verifier_signaux() tourne à chaque tick, soit toutes les 5 s."""
        self._position(minutes=220)

        self._verifier()
        self._verifier()
        self._verifier()

        self.assertEqual(Alerte.objects.count(), 1)
        # Et un seul e-mail : c'est l'exigence qui justifie le champ
        # email_envoye_le.
        self.assertEqual(len(mail.outbox), 1)

    def test_pas_d_alerte_pour_un_camion_qui_remonte(self):
        self._position(minutes=2, vitesse="58")
        bilan = self._verifier()
        self.assertEqual(bilan["ouvertes"], [])
        self.assertEqual(Alerte.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_pas_d_alerte_sans_aucun_releve(self):
        """« Jamais rien reçu » est un boîtier neuf, pas une panne."""
        bilan = self._verifier()
        self.assertEqual(bilan["ouvertes"], [])
        self.assertEqual(Alerte.objects.count(), 0)

    def test_pas_d_alerte_pour_un_camion_sans_boitier(self):
        sans_boitier = creer_vehicule(
            immatriculation="ZZ 0001 RB", fournisseur_gps=None
        )
        PositionGPS.objects.create(
            vehicule=sans_boitier,
            latitude=Decimal("6.370300"),
            longitude=Decimal("2.391200"),
            vitesse_kmh=Decimal("0"),
            horodatage=self.maintenant - timedelta(minutes=300),
        )
        self._verifier()
        self.assertFalse(Alerte.objects.filter(vehicule=sans_boitier).exists())

    def test_pas_d_alerte_pour_un_camion_sorti_de_la_flotte(self):
        self._position(minutes=220)
        self.vehicule.actif = False
        self.vehicule.save(update_fields=["actif"])

        self._verifier()
        self.assertEqual(Alerte.objects.count(), 0)

    # --- Résolution ----------------------------------------------------------

    def test_l_alerte_se_resout_au_retour_du_signal(self):
        self._position(minutes=220)
        self._verifier()
        alerte = Alerte.objects.get()

        # Le boîtier remonte à nouveau.
        self._position(minutes=1, vitesse="54")
        bilan = self._verifier()

        self.assertEqual(len(bilan["resolues"]), 1)
        alerte.refresh_from_db()
        self.assertFalse(alerte.est_ouverte)
        self.assertEqual(alerte.resolue_le, self.maintenant)

    def test_une_nouvelle_panne_apres_resolution_ouvre_une_nouvelle_alerte(self):
        """La contrainte d'unicité ne porte que sur les alertes ouvertes."""
        self._position(minutes=220)
        self._verifier()
        self._position(minutes=1, vitesse="54")
        self._verifier()

        # Le boîtier se tait de nouveau : l'ancien relevé récent vieillit.
        self.maintenant = self.maintenant + timedelta(minutes=90)
        self._verifier()

        self.assertEqual(Alerte.objects.count(), 2)
        self.assertEqual(Alerte.objects.filter(resolue_le__isnull=True).count(), 1)

    # --- Résistance aux pannes d'envoi --------------------------------------

    def test_un_echec_d_envoi_ne_perd_pas_l_alerte(self):
        """Le serveur SMTP est injoignable : l'alerte doit survivre.

        C'est l'exigence la plus importante de ce fichier. Une notification
        est une conséquence ; l'alerte, elle, est le travail. Si un réseau
        capricieux pouvait annuler la détection, l'application mentirait
        précisément quand elle est le plus utile.
        """
        self._position(minutes=220)

        with patch(
            "fleet.services.send_mail", side_effect=OSError("serveur injoignable")
        ):
            bilan = self._verifier()

        self.assertEqual(len(bilan["ouvertes"]), 1)
        alerte = Alerte.objects.get()
        self.assertTrue(alerte.est_ouverte)
        # L'envoi n'a pas eu lieu : la date reste vide, ce qui fera réessayer.
        self.assertIsNone(alerte.email_envoye_le)

    def test_l_envoi_est_reessaye_au_passage_suivant(self):
        self._position(minutes=220)
        with patch("fleet.services.send_mail", side_effect=OSError("injoignable")):
            self._verifier()
        self.assertEqual(len(mail.outbox), 0)

        # Le serveur SMTP est revenu.
        bilan = self._verifier()

        self.assertEqual(len(bilan["reessayees"]), 1)
        self.assertEqual(len(mail.outbox), 1)
        alerte = Alerte.objects.get()
        self.assertIsNotNone(alerte.email_envoye_le)
        # Toujours une seule alerte : la reprise notifie, elle ne duplique pas.
        self.assertEqual(Alerte.objects.count(), 1)

    @override_settings(FLEETFLOW_EMAIL_GERANT="")
    def test_sans_adresse_de_gerant_l_alerte_est_quand_meme_ouverte(self):
        """Une configuration incomplète ne doit pas faire perdre la détection."""
        self._position(minutes=220)

        with self.assertLogs("fleet.services", level="WARNING") as journal:
            self._verifier()

        self.assertEqual(Alerte.objects.count(), 1)
        self.assertIsNone(Alerte.objects.get().email_envoye_le)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("FLEETFLOW_EMAIL_GERANT", " ".join(journal.output))

    def test_sans_capture_des_rappels_aucun_email_ne_part(self):
        """Démonstration du piège documenté en tête de fichier.

        Hors de captureOnCommitCallbacks, la transaction du test n'est jamais
        validée : la notification programmée par transaction.on_commit n'est
        pas exécutée. Ce test existe pour que le piège soit écrit noir sur
        blanc, et pas seulement en commentaire.
        """
        self._position(minutes=220)
        services.verifier_signaux(maintenant=self.maintenant)

        self.assertEqual(Alerte.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    # --- Requêtes ------------------------------------------------------------

    def test_le_balayage_ne_grossit_pas_avec_la_flotte(self):
        """Sept camions équipés ne doivent pas coûter plus qu'un seul.

        On compare deux mesures au lieu de figer un nombre : la première
        version de ce test attendait quatre requêtes et en comptait sept,
        parce qu'un @transaction.atomic imbriqué dans la transaction du test
        ajoute un point de sauvegarde et sa libération. Ce n'est pas ce que le
        test cherche à garantir — ce qui compte, c'est que le compte ne croisse
        pas avec la taille de la flotte.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._position(minutes=220)
        # Premier passage hors mesure : il ouvre l'alerte. Sans cette
        # stabilisation, les deux mesures compareraient deux situations
        # différentes — l'une qui ouvre une alerte, l'autre qui n'a rien à
        # faire — et c'est l'erreur qu'avait ma première version de ce test.
        services.verifier_signaux(maintenant=self.maintenant)

        with CaptureQueriesContext(connection) as avec_un:
            services.verifier_signaux(maintenant=self.maintenant)

        # Les six autres camions n'ont aucun relevé : ils sont « aucune
        # donnée », donc aucune alerte supplémentaire n'est ouverte.
        for index in range(6):
            creer_vehicule(
                immatriculation=f"YY {3000 + index} RB",
                fournisseur_gps=self.fournisseur,
                boitier_id=f"CT-9{index}",
            )
        with CaptureQueriesContext(connection) as avec_sept:
            services.verifier_signaux(maintenant=self.maintenant)

        self.assertEqual(len(avec_un), len(avec_sept))


@override_settings(FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=30)
class BandeauEtPageAlertesTest(TestCase):
    """Le bandeau de toutes les pages, et la page /alertes/."""

    def setUp(self):
        self.utilisateur = get_user_model().objects.create_user(
            username="gestionnaire"
        )
        self.client.force_login(self.utilisateur)
        self.vehicule = creer_vehicule(immatriculation="GH 3456 RB")
        self.alerte = Alerte.objects.create(
            type_alerte=Alerte.TypeAlerte.SANS_SIGNAL,
            vehicule=self.vehicule,
            message="Le boîtier de GH 3456 RB ne remonte plus de données GPS.",
            ouverte_le=timezone.now() - timedelta(hours=3, minutes=40),
        )

    def test_le_bandeau_apparait_sur_toutes_les_pages(self):
        for nom in [
            "fleet:vehicule_liste",
            "fleet:chauffeur_liste",
            "fleet:mission_liste",
            "fleet:document_liste",
        ]:
            with self.subTest(vue=nom):
                reponse = self.client.get(reverse(nom))
                self.assertContains(reponse, "ne remonte plus de")
                self.assertContains(reponse, "GH 3456 RB")
                self.assertContains(reponse, "ff-bandeau--rouge")

    def test_le_compteur_porte_la_date_en_iso(self):
        """C'est cet attribut que le script relit pour faire avancer la durée."""
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertContains(reponse, "data-depuis=")
        self.assertContains(reponse, "bandeau-alertes")
        self.assertContains(reponse, "compteur-alerte.js")

    def test_pas_de_bandeau_quand_tout_va_bien(self):
        self.alerte.resolue_le = timezone.now()
        self.alerte.save(update_fields=["resolue_le"])
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertNotContains(reponse, "ne remonte plus de")

    def test_le_bandeau_ne_coute_qu_une_requete(self):
        """Un context processor s'exécute à chaque page : son coût est partout."""
        from fleet.context_processors import alertes

        requete = type("Requete", (), {"user": self.utilisateur})()
        with self.assertNumQueries(1):
            contexte = alertes(requete)
        self.assertEqual(len(contexte["alertes_sans_signal"]), 1)

    def test_aucune_requete_pour_un_visiteur_anonyme(self):
        from django.contrib.auth.models import AnonymousUser
        from fleet.context_processors import alertes

        requete = type("Requete", (), {"user": AnonymousUser()})()
        with self.assertNumQueries(0):
            self.assertEqual(alertes(requete)["alertes_sans_signal"], [])

    def test_la_page_des_alertes(self):
        reponse = self.client.get(reverse("fleet:alerte_liste"))
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "Alertes ouvertes")
        self.assertContains(reponse, "GH 3456 RB")
        self.assertContains(reponse, "En attente")

    def test_la_page_montre_les_alertes_resolues_avec_leur_duree(self):
        self.alerte.resolue_le = self.alerte.ouverte_le + timedelta(hours=1)
        self.alerte.save(update_fields=["resolue_le"])

        reponse = self.client.get(reverse("fleet:alerte_liste"))
        self.assertContains(reponse, "Alertes résolues")
        # On vise le mot et non la graphie exacte : timesince insère une espace
        # insécable que la version de Django peut changer, et ce n'est pas ce
        # que le test cherche à garantir.
        self.assertContains(reponse, "heure")
        self.assertContains(reponse, self.alerte.resolue_le.strftime("%d/%m/%Y"))

    def test_le_fragment_du_bandeau(self):
        reponse = self.client.get(reverse("fleet:alerte_bandeau"))
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, "GH 3456 RB")
        # Un fragment, pas une page : ni barre latérale, ni <html>.
        self.assertNotContains(reponse, "ff-sidebar")
        self.assertNotContains(reponse, "<!DOCTYPE html>")

    def test_le_fragment_exige_une_connexion(self):
        self.client.logout()
        reponse = self.client.get(reverse("fleet:alerte_bandeau"))
        self.assertEqual(reponse.status_code, 302)
        self.assertIn(reverse("login"), reponse.url)

    def test_le_menu_compte_les_alertes(self):
        reponse = self.client.get(reverse("fleet:vehicule_liste"))
        self.assertContains(reponse, "Alertes")
        self.assertContains(reponse, "ff-badge ff-badge--rouge")


class CommandeEtAlertesTest(TestCase):
    """La simulation ouvre et résout les alertes au fil des ticks."""

    @override_settings(
        FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=1,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FLEETFLOW_EMAIL_GERANT=GERANT,
    )
    def test_un_tick_ouvre_l_alerte_d_un_boitier_muet(self):
        from io import StringIO

        from django.core.management import call_command

        fournisseur = FournisseurGPS.objects.create(nom="Orange Fleet")
        vehicule = creer_vehicule(
            immatriculation="AB 1234 RB",
            fournisseur_gps=fournisseur,
            boitier_id="OF-1",
            itineraire="cotonou_parakou",
        )
        chauffeur = creer_chauffeur()
        services.creer_mission(
            vehicule=vehicule,
            chauffeur=chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=timezone.localdate(),
        )
        # Un relevé vieux de dix minutes, alors que le seuil est à une minute.
        PositionGPS.objects.create(
            vehicule=vehicule,
            latitude=Decimal("6.370300"),
            longitude=Decimal("2.391200"),
            vitesse_kmh=Decimal("0"),
            horodatage=timezone.now() - timedelta(minutes=10),
        )
        services.basculer_signal(vehicule, coupe=True)

        sortie = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("simuler_positions", stdout=sortie, stderr=sortie)

        self.assertIn("ALERTE ouverte", sortie.getvalue())
        self.assertEqual(Alerte.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        # La sortie reste en ASCII, y compris les lignes d'alerte.
        sortie.getvalue().encode("ascii")
