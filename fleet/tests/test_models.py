"""Tests des propriétés calculées des modèles.

Ces propriétés dépendent de la date du jour. Les tests ne figent donc jamais
une date en dur : ils construisent leurs données par rapport à
timezone.localdate(), exactement comme le code testé. Un test écrit avec
« 2026-10-15 » passerait aujourd'hui et échouerait le mois prochain.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from fleet.models import Document, Mission, PleinCarburant, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule


class VehiculeEcheancesTest(TestCase):
    """Les propriétés de Vehicule qui relisent les documents."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule()

    def test_assurance_valide(self):
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.ASSURANCE,
            date_expiration=self.aujourdhui + timedelta(days=10),
        )
        self.assertEqual(self.vehicule.jours_avant_fin_assurance, 10)
        self.assertFalse(self.vehicule.assurance_expiree)

    def test_assurance_expiree(self):
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.ASSURANCE,
            date_expiration=self.aujourdhui - timedelta(days=3),
        )
        self.assertEqual(self.vehicule.jours_avant_fin_assurance, -3)
        self.assertTrue(self.vehicule.assurance_expiree)

    def test_sans_attestation_la_reponse_est_inconnue(self):
        """Aucune assurance enregistrée : None, et surtout pas False.

        C'est le cas refusé le plus important de ce fichier : renvoyer False
        ferait passer pour assuré un véhicule dont le dossier est vide.
        """
        self.assertIsNone(self.vehicule.date_fin_assurance)
        self.assertIsNone(self.vehicule.jours_avant_fin_assurance)
        self.assertIsNone(self.vehicule.assurance_expiree)

    def test_le_document_en_vigueur_est_le_plus_lointain(self):
        """Après renouvellement, c'est la nouvelle attestation qui fait foi."""
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.ASSURANCE,
            date_expiration=self.aujourdhui - timedelta(days=5),
        )
        nouvelle = Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.ASSURANCE,
            date_expiration=self.aujourdhui + timedelta(days=360),
        )
        self.assertEqual(self.vehicule.document_assurance, nouvelle)
        self.assertFalse(self.vehicule.assurance_expiree)

    def test_une_carte_grise_n_est_pas_une_assurance(self):
        """Un document d'un autre type ne renseigne pas l'assurance."""
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.CARTE_GRISE,
            date_expiration=None,
        )
        self.assertIsNone(self.vehicule.document_assurance)

    def test_visite_technique(self):
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.VISITE_TECHNIQUE,
            date_expiration=self.aujourdhui + timedelta(days=45),
        )
        self.assertEqual(
            self.vehicule.date_visite_technique,
            self.aujourdhui + timedelta(days=45),
        )

    def test_lecture_des_echeances_sans_requete_supplementaire(self):
        """Avec prefetch_related, les propriétés ne relancent aucune requête.

        C'est la raison d'être du filtrage en Python dans _dernier_document :
        ce test échouerait si la propriété utilisait un .filter() SQL.
        """
        Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.ASSURANCE,
            date_expiration=self.aujourdhui + timedelta(days=100),
        )
        vehicules = list(Vehicule.objects.prefetch_related("documents"))
        with self.assertNumQueries(0):
            for vehicule in vehicules:
                vehicule.jours_avant_fin_assurance
                vehicule.date_visite_technique


class DocumentTest(TestCase):
    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule()

    def test_jours_avant_expiration(self):
        document = Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.LICENCE_TRANSPORT,
            date_expiration=self.aujourdhui + timedelta(days=7),
        )
        self.assertEqual(document.jours_avant_expiration, 7)

    def test_piece_sans_echeance(self):
        document = Document.objects.create(
            vehicule=self.vehicule,
            type_document=Document.TypeDocument.CARTE_GRISE,
            date_expiration=None,
        )
        self.assertIsNone(document.jours_avant_expiration)


class ChauffeurPermisTest(TestCase):
    def test_permis_valide(self):
        chauffeur = creer_chauffeur(
            date_expiration_permis=timezone.localdate() + timedelta(days=40)
        )
        self.assertEqual(chauffeur.jours_avant_expiration_permis, 40)
        self.assertFalse(chauffeur.permis_expire)

    def test_permis_expire(self):
        chauffeur = creer_chauffeur(
            date_expiration_permis=timezone.localdate() - timedelta(days=1)
        )
        self.assertEqual(chauffeur.jours_avant_expiration_permis, -1)
        self.assertTrue(chauffeur.permis_expire)


class MissionDistanceTest(TestCase):
    def setUp(self):
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def _mission(self, **champs):
        valeurs = {
            "vehicule": self.vehicule,
            "chauffeur": self.chauffeur,
            "depart": "Cotonou",
            "destination": "Parakou",
            "date_depart": timezone.localdate(),
            "km_depart": 100_000,
        }
        valeurs.update(champs)
        return Mission.objects.create(**valeurs)

    def test_distance_d_une_mission_terminee(self):
        mission = self._mission(
            km_arrivee=100_430,
            date_arrivee=timezone.localdate(),
            statut=Mission.Statut.TERMINEE,
        )
        self.assertEqual(mission.distance, 430)

    def test_distance_inconnue_tant_que_la_mission_n_est_pas_cloturee(self):
        """None et non 0 : « pas encore relevé » n'est pas « zéro kilomètre »."""
        mission = self._mission(statut=Mission.Statut.EN_COURS)
        self.assertIsNone(mission.distance)


class ContraintesBaseTest(TestCase):
    """Les CheckConstraint posées en base, indépendamment des services.

    Ces tests écrivent volontairement avec Mission.objects.create(), sans
    passer par services.py, pour prouver que la base refuse elle aussi. C'est
    le filet de sécurité qui protège l'admin Django, un script d'import ou une
    session de shell.

    Chaque cas est enveloppé dans transaction.atomic() : une IntegrityError
    laisse la transaction du test dans un état inutilisable, et le bloc atomic
    imbriqué la circonscrit au seul appel fautif.
    """

    def setUp(self):
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()
        self.aujourdhui = timezone.localdate()

    def test_km_arrivee_inferieur_au_depart_refuse_par_la_base(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Mission.objects.create(
                    vehicule=self.vehicule,
                    chauffeur=self.chauffeur,
                    depart="Cotonou",
                    destination="Bohicon",
                    date_depart=self.aujourdhui,
                    date_arrivee=self.aujourdhui,
                    km_depart=100_000,
                    km_arrivee=99_000,
                    statut=Mission.Statut.TERMINEE,
                )

    def test_mission_en_cours_sans_km_arrivee_acceptee(self):
        """Le cas NULL doit rester permis, sinon plus aucune mission en cours."""
        mission = Mission.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Bohicon",
            date_depart=self.aujourdhui,
            km_depart=100_000,
            statut=Mission.Statut.EN_COURS,
        )
        self.assertIsNone(mission.km_arrivee)

    def test_date_arrivee_avant_depart_refusee_par_la_base(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Mission.objects.create(
                    vehicule=self.vehicule,
                    chauffeur=self.chauffeur,
                    depart="Cotonou",
                    destination="Bohicon",
                    date_depart=self.aujourdhui,
                    date_arrivee=self.aujourdhui - timedelta(days=2),
                    km_depart=100_000,
                    km_arrivee=100_130,
                    statut=Mission.Statut.TERMINEE,
                )

    def test_plein_de_zero_litre_refuse_par_la_base(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PleinCarburant.objects.create(
                    vehicule=self.vehicule,
                    chauffeur=self.chauffeur,
                    date=self.aujourdhui,
                    litres=0,
                    prix_litre=700,
                    km_compteur=100_500,
                )
