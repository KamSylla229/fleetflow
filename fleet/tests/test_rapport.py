"""Tests du rapport quotidien : kilomètres mesurés, contenu, et envoi.

Le test central est celui de la somme des kilomètres : il refait le calcul à
la main, segment par segment, avec la même fonction haversine que le service.
Comparer le résultat à un nombre écrit en dur ne prouverait rien — il
suffirait de recopier le nombre que le code produit, bug compris.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from fleet import services
from fleet.itineraires import distance_km
from fleet.models import Alerte, Document, FournisseurGPS, PleinCarburant, PositionGPS
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule

GERANT = "gerant@fleetflow.invalid"

# Quatre points réels le long de la RNIE 2, du sud vers le nord.
TRAJET = (
    (6.3703, 2.3912),   # Cotonou
    (6.4486, 2.3556),   # Abomey-Calavi
    (6.6658, 2.1511),   # Allada
    (7.1781, 2.0667),   # Bohicon
)


class KilometresParcourusTest(TestCase):
    """La mesure : somme des cordes entre relevés successifs."""

    def setUp(self):
        self.jour = timezone.localdate()
        self.midi = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")
        self.chauffeur = creer_chauffeur()

    def _position(self, point, minutes, jour=None):
        """Un relevé aux coordonnées données, `minutes` après midi."""
        latitude, longitude = point
        horodatage = (self.midi if jour is None else jour) + timedelta(minutes=minutes)
        return PositionGPS.objects.create(
            vehicule=self.vehicule,
            latitude=Decimal(str(latitude)),
            longitude=Decimal(str(longitude)),
            vitesse_kmh=Decimal("60"),
            horodatage=horodatage,
        )

    def test_la_somme_correspond_au_calcul_a_la_main(self):
        """L'exigence : le total du rapport est la somme des segments.

        On recalcule les trois segments un par un avec la même fonction
        haversine, puis on compare. Écrire le total attendu en dur ne
        prouverait rien : on recopierait le chiffre que le code produit, bug
        compris.
        """
        for rang, point in enumerate(TRAJET):
            self._position(point, minutes=rang * 20)

        attendu = (
            distance_km(TRAJET[0], TRAJET[1])
            + distance_km(TRAJET[1], TRAJET[2])
            + distance_km(TRAJET[2], TRAJET[3])
        )

        mesure = services.kilometres_parcourus(self.jour)

        self.assertEqual(len(mesure), 1)
        vehicule, km = mesure[0]
        self.assertEqual(vehicule, self.vehicule)
        self.assertAlmostEqual(km, round(attendu, 1), places=1)
        # Et l'ordre de grandeur : Cotonou - Bohicon fait une centaine de km.
        self.assertGreater(km, 80)
        self.assertLess(km, 120)

    def test_un_seul_releve_ne_fait_aucun_kilometre(self):
        """Un point isolé ne dit rien d'une distance : zéro, pas None.

        Ici zéro est juste : le camion a été vu une fois, aucun déplacement
        n'est mesurable entre un point et lui-même.
        """
        self._position(TRAJET[0], minutes=0)
        self.assertEqual(services.kilometres_parcourus(self.jour)[0][1], 0.0)

    def test_un_camion_immobile_fait_zero(self):
        for rang in range(4):
            self._position(TRAJET[0], minutes=rang * 10)
        self.assertEqual(services.kilometres_parcourus(self.jour)[0][1], 0.0)

    def test_seules_les_positions_du_jour_comptent(self):
        """Les relevés d'hier ne doivent pas gonfler le total d'aujourd'hui."""
        hier = self.midi - timedelta(days=1)
        for rang, point in enumerate(TRAJET):
            self._position(point, minutes=rang * 20, jour=hier)
        self._position(TRAJET[0], minutes=0)
        self._position(TRAJET[1], minutes=20)

        du_jour = services.kilometres_parcourus(self.jour)[0][1]
        de_la_veille = services.kilometres_parcourus(self.jour - timedelta(days=1))[0][1]

        self.assertAlmostEqual(
            du_jour, round(distance_km(TRAJET[0], TRAJET[1]), 1), places=1
        )
        self.assertGreater(de_la_veille, du_jour)

    def test_chaque_camion_a_son_total(self):
        autre = creer_vehicule(immatriculation="AC 4821 RB")
        self._position(TRAJET[0], minutes=0)
        self._position(TRAJET[3], minutes=30)
        for rang, point in enumerate(TRAJET[:2]):
            PositionGPS.objects.create(
                vehicule=autre,
                latitude=Decimal(str(point[0])),
                longitude=Decimal(str(point[1])),
                vitesse_kmh=Decimal("50"),
                horodatage=self.midi + timedelta(minutes=rang * 10),
            )

        mesure = dict(
            (vehicule.immatriculation, km)
            for vehicule, km in services.kilometres_parcourus(self.jour)
        )
        self.assertEqual(set(mesure), {"AB 1234 RB", "AC 4821 RB"})
        self.assertGreater(mesure["AB 1234 RB"], mesure["AC 4821 RB"])

    def test_le_plus_roulant_est_en_tete(self):
        """L'ordre dans lequel un gérant veut lire la liste."""
        petit = creer_vehicule(immatriculation="ZZ 0001 RB")
        self._position(TRAJET[0], minutes=0)
        self._position(TRAJET[3], minutes=30)
        for rang, point in enumerate(TRAJET[:2]):
            PositionGPS.objects.create(
                vehicule=petit,
                latitude=Decimal(str(point[0])),
                longitude=Decimal(str(point[1])),
                vitesse_kmh=Decimal("50"),
                horodatage=self.midi + timedelta(minutes=rang * 5),
            )

        mesure = services.kilometres_parcourus(self.jour)
        self.assertEqual(mesure[0][0], self.vehicule)

    def test_une_journee_sans_releve(self):
        self.assertEqual(services.kilometres_parcourus(self.jour), [])

    def test_une_seule_requete(self):
        for rang, point in enumerate(TRAJET):
            self._position(point, minutes=rang * 20)
        for index in range(5):
            autre = creer_vehicule(immatriculation=f"YY {8000 + index} RB")
            PositionGPS.objects.create(
                vehicule=autre,
                latitude=Decimal("6.370300"),
                longitude=Decimal("2.391200"),
                vitesse_kmh=Decimal("0"),
                horodatage=self.midi,
            )
        with self.assertNumQueries(1):
            services.kilometres_parcourus(self.jour)


class DonneesRapportTest(TestCase):
    """Le dictionnaire que la commande et les deux gabarits partagent."""

    def setUp(self):
        self.jour = timezone.localdate()
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")
        self.chauffeur = creer_chauffeur()

    def test_les_pleins_du_jour_et_leurs_totaux(self):
        for km, litres in ((100_000, "40"), (100_500, "45")):
            PleinCarburant.objects.create(
                vehicule=self.vehicule,
                chauffeur=self.chauffeur,
                date=self.jour,
                litres=Decimal(litres),
                prix_litre=Decimal("700"),
                km_compteur=km,
            )

        donnees = services.donnees_rapport_quotidien(self.jour)

        self.assertEqual(len(donnees["pleins"]), 2)
        self.assertEqual(donnees["total_litres"], Decimal("85"))
        self.assertEqual(donnees["total_cout"], Decimal("59500"))
        # La consommation est annotée : 45 L pour 500 km.
        consommations = [plein.consommation for plein in donnees["pleins"]]
        self.assertIn(Decimal("9.00"), consommations)

    def test_les_pleins_d_un_autre_jour_sont_exclus(self):
        PleinCarburant.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.jour - timedelta(days=3),
            litres=Decimal("40"),
            prix_litre=Decimal("700"),
            km_compteur=100_000,
        )
        donnees = services.donnees_rapport_quotidien(self.jour)
        self.assertEqual(len(donnees["pleins"]), 0)
        self.assertEqual(donnees["total_litres"], Decimal("0"))

    def test_les_echeances_sont_datees_du_jour_du_rapport(self):
        """Un rapport d'hier décrit la situation d'hier, pas celle du jour."""
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=-10)

        aujourdhui = services.donnees_rapport_quotidien(self.jour)
        avant_hier = services.donnees_rapport_quotidien(self.jour - timedelta(days=2))

        self.assertEqual(aujourdhui["classement"]["depassees"][0].jours, -10)
        self.assertEqual(avant_hier["classement"]["depassees"][0].jours, -8)

    def test_les_alertes_ouvertes_sont_listees(self):
        Alerte.objects.create(
            type_alerte=Alerte.TypeAlerte.SANS_SIGNAL,
            vehicule=self.vehicule,
            message="Le boîtier de AB 1234 RB ne remonte plus rien.",
        )
        Alerte.objects.create(
            type_alerte=Alerte.TypeAlerte.ECHEANCE_PERMIS,
            chauffeur=self.chauffeur,
            message="Permis à renouveler.",
            resolue_le=timezone.now(),
        )
        donnees = services.donnees_rapport_quotidien(self.jour)
        # Seules les ouvertes : une alerte résolue n'a plus rien à signaler.
        self.assertEqual(len(donnees["alertes"]), 1)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FLEETFLOW_EMAIL_GERANT=GERANT,
)
class CommandeRapportQuotidienTest(TestCase):
    def setUp(self):
        self.jour = timezone.localdate()
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")
        self.chauffeur = creer_chauffeur()
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=-12)
        PleinCarburant.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.jour,
            litres=Decimal("45"),
            prix_litre=Decimal("715"),
            km_compteur=100_000,
        )

    def _appeler(self, *args):
        sortie = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("rapport_quotidien", *args, stdout=sortie, stderr=sortie)
        return sortie.getvalue()

    def test_par_defaut_le_rapport_s_affiche_sans_rien_envoyer(self):
        """Une commande qui écrit dans une boîte sans qu'on l'ait demandé est
        une commande qu'on n'ose plus lancer pour voir."""
        sortie = self._appeler()

        self.assertIn("rapport du", sortie)
        self.assertIn("AB 1234 RB", sortie)
        self.assertIn("Ajoutez --email", sortie)
        # Seule l'alerte d'échéance a envoyé un message, pas le rapport.
        self.assertEqual(
            [message.subject for message in mail.outbox],
            [f"[FleetFlow] Assurance — AB 1234 RB : dépassée depuis le "
             f"{(self.jour - timedelta(days=12)):%d/%m/%Y}"],
        )

    def test_le_rapport_contient_les_quatre_sections(self):
        sortie = self._appeler()
        for section in [
            "KILOMÉTRAGE DE LA JOURNÉE",
            "CARBURANT",
            "ALERTES OUVERTES",
            "ÉCHÉANCES",
        ]:
            with self.subTest(section=section):
                self.assertIn(section, sortie)
        self.assertIn("45.00 L", sortie)
        self.assertIn("Dépassées (1)", sortie)

    def test_la_sortie_console_est_encodable_en_cp1252(self):
        """La console Windows écrit en cp1252.

        Les accents y passent — c'est la flèche et l'emoji qui ne passent pas,
        comme le seed l'a appris le 30/09. Ce test garde la propriété sans
        interdire le français correct.
        """
        self._appeler().encode("cp1252")

    def test_la_commande_verifie_les_echeances(self):
        """Sans cela, le rapport annoncerait une pièce dépassée sans alerte."""
        self.assertEqual(Alerte.objects.count(), 0)
        self._appeler()
        self.assertEqual(
            Alerte.objects.filter(
                type_alerte=Alerte.TypeAlerte.ECHEANCE, resolue_le__isnull=True
            ).count(),
            1,
        )

    def test_avec_email_le_rapport_part(self):
        self._appeler("--email")

        rapports = [m for m in mail.outbox if "Rapport du" in m.subject]
        self.assertEqual(len(rapports), 1)
        message = rapports[0]
        self.assertEqual(message.to, [GERANT])
        self.assertIn("KILOMÉTRAGE", message.body)
        # Une variante HTML, le texte restant le corps principal : un client
        # qui n'affiche pas le HTML montre un rapport lisible.
        self.assertEqual(len(message.alternatives), 1)
        contenu_html, type_mime = message.alternatives[0]
        self.assertEqual(type_mime, "text/html")
        self.assertIn("<html", contenu_html)
        self.assertIn("AB 1234 RB", contenu_html)

    def test_avec_a_le_destinataire_est_remplace(self):
        self._appeler("--email", "--a", "chef@exemple.test")
        rapports = [m for m in mail.outbox if "Rapport du" in m.subject]
        self.assertEqual(rapports[0].to, ["chef@exemple.test"])

    @override_settings(FLEETFLOW_EMAIL_GERANT="")
    def test_sans_destinataire_l_erreur_est_claire(self):
        with self.assertRaises(CommandError) as contexte:
            self._appeler("--email")
        self.assertIn("FLEETFLOW_EMAIL_GERANT", str(contexte.exception))

    @override_settings(FLEETFLOW_EMAIL_GERANT="")
    def test_sans_destinataire_l_affichage_fonctionne_quand_meme(self):
        """Lire le rapport ne demande aucune configuration d'e-mail."""
        sortie = self._appeler()
        self.assertIn("rapport du", sortie)

    def test_une_date_illisible_est_refusee(self):
        for valeur in ("hier", "01/10/2026", "2026-13-45"):
            with self.subTest(valeur=valeur):
                with self.assertRaises(CommandError) as contexte:
                    self._appeler("--date", valeur)
                self.assertIn("Format attendu", str(contexte.exception))

    def test_une_journee_passee(self):
        sortie = self._appeler("--date", (self.jour - timedelta(days=4)).isoformat())
        self.assertIn((self.jour - timedelta(days=4)).strftime("%d/%m/%Y"), sortie)
        self.assertIn("Aucun relevé GPS", sortie)

    def test_un_echec_d_envoi_fait_echouer_la_commande(self):
        """Contrairement aux alertes : une tâche planifiée qui n'envoie plus
        rien doit échouer bruyamment, sinon personne ne s'en aperçoit."""
        from unittest.mock import patch

        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=OSError("injoignable"),
        ):
            with self.assertRaises(CommandError) as contexte:
                self._appeler("--email")
        self.assertIn("L'envoi a échoué", str(contexte.exception))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FLEETFLOW_EMAIL_GERANT=GERANT,
)
class RapportAvecFlotteTest(TestCase):
    """Un rapport sur des données plus proches de la démonstration."""

    def test_le_rapport_tient_avec_une_flotte_et_des_alertes(self):
        jour = timezone.localdate()
        fournisseur = FournisseurGPS.objects.create(nom="Cartrack")
        chauffeur = creer_chauffeur()

        for index, point in enumerate(TRAJET):
            vehicule = creer_vehicule(
                immatriculation=f"AB {1000 + index} RB",
                fournisseur_gps=fournisseur,
                boitier_id=f"CT-{index}",
            )
            creer_document(vehicule, Document.TypeDocument.ASSURANCE, jours=index * 20 - 10)
            for rang in range(3):
                PositionGPS.objects.create(
                    vehicule=vehicule,
                    latitude=Decimal(str(TRAJET[rang][0])),
                    longitude=Decimal(str(TRAJET[rang][1])),
                    vitesse_kmh=Decimal("60"),
                    horodatage=timezone.now() - timedelta(minutes=30 - rang * 10),
                )

        sortie = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("rapport_quotidien", "--email", stdout=sortie, stderr=sortie)

        self.assertIn("Rapport envoyé à", sortie.getvalue())
        rapports = [m for m in mail.outbox if "Rapport du" in m.subject]
        self.assertEqual(len(rapports), 1)
        self.assertIn("Total", rapports[0].body)
        sortie.getvalue().encode("cp1252")
