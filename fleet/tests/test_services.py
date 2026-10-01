"""Tests des fonctions de service : un cas nominal et des cas refusés.

Chaque fonction de services.py est testée deux fois au moins : une fois dans
les conditions prévues, une fois au moins dans un cas qu'elle doit refuser. Un
test qui ne vérifie que le chemin heureux ne prouve rien des règles de gestion,
qui sont justement faites pour dire non.

Les refus sont vérifiés avec assertRaises(ValidationError). Quand le refus doit
aussi laisser la base intacte, le test le contrôle explicitement : une
validation qui lève après avoir écrit serait un bug plus grave que l'absence de
validation.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from fleet import services
from fleet.models import Document, Entretien, Mission, PleinCarburant, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule


class StatutEcheanceTest(TestCase):
    """La règle d'échéance, partagée par les documents et les permis."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()

    def test_document_valide(self):
        statut = services.statut_echeance(self.aujourdhui + timedelta(days=90))
        self.assertEqual(statut.code, services.ECHEANCE_VALIDE)
        self.assertEqual(statut.ton, "vert")
        self.assertFalse(statut.est_alerte)

    def test_document_bientot_expire(self):
        statut = services.statut_echeance(self.aujourdhui + timedelta(days=12))
        self.assertEqual(statut.code, services.ECHEANCE_BIENTOT)
        self.assertEqual(statut.jours, 12)
        self.assertTrue(statut.est_alerte)

    def test_document_expire(self):
        statut = services.statut_echeance(self.aujourdhui - timedelta(days=5))
        self.assertEqual(statut.code, services.ECHEANCE_EXPIREE)
        self.assertEqual(statut.ton, "rouge")
        self.assertTrue(statut.est_alerte)

    def test_les_bornes_du_seuil(self):
        """Le jour du seuil alerte encore ; le lendemain, non.

        Les erreurs de comparaison (< au lieu de <=) se cachent exactement
        là : sur la valeur limite, jamais au milieu de l'intervalle.
        """
        seuil = services.SEUIL_ALERTE_ECHEANCE_JOURS
        juste_dedans = services.statut_echeance(
            self.aujourdhui + timedelta(days=seuil)
        )
        juste_dehors = services.statut_echeance(
            self.aujourdhui + timedelta(days=seuil + 1)
        )
        self.assertEqual(juste_dedans.code, services.ECHEANCE_BIENTOT)
        self.assertEqual(juste_dehors.code, services.ECHEANCE_VALIDE)

    def test_expire_aujourd_hui_est_une_alerte_pas_une_expiration(self):
        statut = services.statut_echeance(self.aujourdhui)
        self.assertEqual(statut.code, services.ECHEANCE_BIENTOT)
        self.assertEqual(statut.jours, 0)

    def test_piece_sans_echeance(self):
        statut = services.statut_echeance(None)
        self.assertEqual(statut.code, services.ECHEANCE_SANS)
        self.assertIsNone(statut.jours)
        self.assertFalse(statut.est_alerte)

    def test_documents_avec_statut(self):
        vehicule = creer_vehicule()
        creer_document(vehicule, Document.TypeDocument.ASSURANCE, jours=-2)
        creer_document(vehicule, Document.TypeDocument.CARTE_GRISE, date_expiration=None)
        codes = {
            document.type_document: document.statut.code
            for document in services.documents_avec_statut(vehicule)
        }
        self.assertEqual(codes["assurance"], services.ECHEANCE_EXPIREE)
        self.assertEqual(codes["carte_grise"], services.ECHEANCE_SANS)


class CreerMissionTest(TestCase):
    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule(kilometrage=120_000)
        self.chauffeur = creer_chauffeur()

    def _creer(self, **champs):
        valeurs = {
            "vehicule": self.vehicule,
            "chauffeur": self.chauffeur,
            "depart": "Cotonou",
            "destination": "Parakou",
            "date_depart": self.aujourdhui,
        }
        valeurs.update(champs)
        return services.creer_mission(**valeurs)

    # --- Cas nominal ---------------------------------------------------------

    def test_affectation_reussie(self):
        mission = self._creer()

        self.assertEqual(mission.statut, Mission.Statut.EN_COURS)
        # Le relevé de départ est repris du compteur du véhicule.
        self.assertEqual(mission.km_depart, 120_000)

        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.statut, Vehicule.Statut.EN_MISSION)

    def test_le_vehicule_devient_engage(self):
        self._creer()
        self.assertTrue(services.missions_en_cours(vehicule=self.vehicule).exists())

    def test_km_depart_peut_etre_saisi(self):
        mission = self._creer(km_depart=120_450)
        self.assertEqual(mission.km_depart, 120_450)

    # --- Cas refusés ---------------------------------------------------------

    def test_refus_deuxieme_mission_sur_le_meme_vehicule(self):
        """Le cas de la Definition of Done : un camion, une mission à la fois."""
        self._creer()
        autre_chauffeur = creer_chauffeur(nom="Sylvain Dossou", permis="BJ-0002")

        with self.assertRaises(ValidationError) as contexte:
            self._creer(chauffeur=autre_chauffeur)

        self.assertIn("déjà engagé", " ".join(contexte.exception.messages))
        # Le refus ne doit rien avoir écrit : une seule mission en base.
        self.assertEqual(Mission.objects.count(), 1)

    def test_refus_deuxieme_mission_sur_le_meme_chauffeur(self):
        self._creer()
        autre_vehicule = creer_vehicule(immatriculation="AA 0002 RB")

        with self.assertRaises(ValidationError) as contexte:
            self._creer(vehicule=autre_vehicule)

        self.assertIn("déjà en mission", " ".join(contexte.exception.messages))
        self.assertEqual(Mission.objects.count(), 1)

    def test_une_mission_terminee_ne_bloque_plus(self):
        """Seul EN_COURS engage : après clôture, le véhicule repart."""
        mission = self._creer()
        services.cloturer_mission(
            mission, date_arrivee=self.aujourdhui, km_arrivee=120_430
        )
        seconde = self._creer()
        self.assertEqual(Mission.objects.count(), 2)
        self.assertEqual(seconde.statut, Mission.Statut.EN_COURS)

    def test_une_mission_planifiee_ne_bloque_pas(self):
        """Une mission prévue dans trois semaines n'empêche pas de rouler."""
        Mission.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Lomé",
            date_depart=self.aujourdhui + timedelta(days=21),
            km_depart=120_000,
            statut=Mission.Statut.PLANIFIEE,
        )
        mission = self._creer()
        self.assertEqual(mission.statut, Mission.Statut.EN_COURS)

    def test_refus_date_de_depart_future(self):
        with self.assertRaises(ValidationError) as contexte:
            self._creer(date_depart=self.aujourdhui + timedelta(days=1))

        self.assertIn("futur", " ".join(contexte.exception.messages))
        self.assertEqual(Mission.objects.count(), 0)

    def test_refus_vehicule_desactive(self):
        vehicule = creer_vehicule(immatriculation="AA 0003 RB", actif=False)
        with self.assertRaises(ValidationError):
            self._creer(vehicule=vehicule)

    def test_refus_chauffeur_desactive(self):
        chauffeur = creer_chauffeur(nom="Ancien", permis="BJ-0009", actif=False)
        with self.assertRaises(ValidationError):
            self._creer(chauffeur=chauffeur)

    def test_refus_permis_expire(self):
        chauffeur = creer_chauffeur(
            nom="Permis périmé",
            permis="BJ-0010",
            date_expiration_permis=self.aujourdhui - timedelta(days=1),
        )
        with self.assertRaises(ValidationError) as contexte:
            self._creer(chauffeur=chauffeur)

        self.assertIn("permis", " ".join(contexte.exception.messages).lower())

    def test_refus_vehicule_en_maintenance(self):
        vehicule = creer_vehicule(
            immatriculation="AA 0004 RB", statut=Vehicule.Statut.EN_MAINTENANCE
        )
        with self.assertRaises(ValidationError) as contexte:
            self._creer(vehicule=vehicule)

        self.assertIn("maintenance", " ".join(contexte.exception.messages))

    def test_refus_km_depart_inferieur_au_compteur(self):
        """Un compteur ne revient pas en arrière : c'est une faute de saisie."""
        with self.assertRaises(ValidationError):
            self._creer(km_depart=119_000)


class CloturerMissionTest(TestCase):
    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule(kilometrage=120_000)
        self.chauffeur = creer_chauffeur()
        self.mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui - timedelta(days=1),
        )

    # --- Cas nominal ---------------------------------------------------------

    def test_cloture_reussie(self):
        mission = services.cloturer_mission(
            self.mission,
            date_arrivee=self.aujourdhui,
            km_arrivee=120_430,
            commentaire="Livraison conforme.",
        )

        self.assertEqual(mission.statut, Mission.Statut.TERMINEE)
        self.assertEqual(mission.distance, 430)
        self.assertEqual(mission.commentaire, "Livraison conforme.")

        self.vehicule.refresh_from_db()
        # Le kilométrage du véhicule suit le relevé d'arrivée : c'est la
        # vérification centrale de la Definition of Done.
        self.assertEqual(self.vehicule.kilometrage, 120_430)
        self.assertEqual(self.vehicule.statut, Vehicule.Statut.DISPONIBLE)

    def test_le_compteur_ne_recule_jamais(self):
        """Un plein pendant la mission a pu pousser le compteur plus loin."""
        services.enregistrer_plein(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui,
            litres=Decimal("60"),
            prix_litre=Decimal("700"),
            km_compteur=122_000,
        )
        services.cloturer_mission(
            self.mission, date_arrivee=self.aujourdhui, km_arrivee=120_430
        )

        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.kilometrage, 122_000)

    # --- Cas refusés ---------------------------------------------------------

    def test_refus_km_arrivee_inferieur_au_depart(self):
        with self.assertRaises(ValidationError) as contexte:
            services.cloturer_mission(
                self.mission, date_arrivee=self.aujourdhui, km_arrivee=119_000
            )

        self.assertIn("kilométrage", " ".join(contexte.exception.messages))
        self.mission.refresh_from_db()
        self.assertEqual(self.mission.statut, Mission.Statut.EN_COURS)

    def test_refus_km_arrivee_egal_au_depart(self):
        """Égal et non seulement inférieur : une mission parcourt des kilomètres."""
        with self.assertRaises(ValidationError):
            services.cloturer_mission(
                self.mission,
                date_arrivee=self.aujourdhui,
                km_arrivee=self.mission.km_depart,
            )

    def test_refus_date_arrivee_avant_depart(self):
        with self.assertRaises(ValidationError) as contexte:
            services.cloturer_mission(
                self.mission,
                date_arrivee=self.mission.date_depart - timedelta(days=1),
                km_arrivee=120_430,
            )

        self.assertIn("précéder", " ".join(contexte.exception.messages))

    def test_refus_date_arrivee_future(self):
        with self.assertRaises(ValidationError):
            services.cloturer_mission(
                self.mission,
                date_arrivee=self.aujourdhui + timedelta(days=1),
                km_arrivee=120_430,
            )

    def test_refus_double_cloture(self):
        """La deuxième clôture ne doit pas rajouter les kilomètres au compteur."""
        services.cloturer_mission(
            self.mission, date_arrivee=self.aujourdhui, km_arrivee=120_430
        )
        with self.assertRaises(ValidationError) as contexte:
            services.cloturer_mission(
                self.mission, date_arrivee=self.aujourdhui, km_arrivee=120_900
            )

        self.assertIn("en cours", " ".join(contexte.exception.messages))
        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.kilometrage, 120_430)


class PleinCarburantTest(TestCase):
    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule(kilometrage=100_000)
        self.chauffeur = creer_chauffeur()

    def _enregistrer(self, **champs):
        valeurs = {
            "vehicule": self.vehicule,
            "chauffeur": self.chauffeur,
            "date": self.aujourdhui,
            "litres": Decimal("50"),
            "prix_litre": Decimal("700"),
            "km_compteur": 100_500,
        }
        valeurs.update(champs)
        return services.enregistrer_plein(**valeurs)

    # --- Cas nominal ---------------------------------------------------------

    def test_premier_plein_sans_consommation(self):
        """Sans plein précédent, la consommation n'a pas de sens : None."""
        plein, consommation = self._enregistrer()

        self.assertIsNotNone(plein.pk)
        self.assertIsNone(consommation)
        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.kilometrage, 100_500)

    def test_consommation_calculee_au_deuxieme_plein(self):
        self._enregistrer(
            date=self.aujourdhui - timedelta(days=10), km_compteur=100_000
        )
        _, consommation = self._enregistrer(km_compteur=100_500, litres=Decimal("45"))

        # 45 litres pour 500 km = 9,00 L/100 km.
        self.assertEqual(consommation, Decimal("9.00"))

    def test_consommation_en_decimal_exact(self):
        """Le calcul reste en Decimal : pas d'arrondi surprenant de flottant."""
        self._enregistrer(
            date=self.aujourdhui - timedelta(days=10), km_compteur=100_000
        )
        _, consommation = self._enregistrer(km_compteur=100_300, litres=Decimal("10"))

        # 10 / 300 * 100 = 3,3333… arrondi à 3,33.
        self.assertEqual(consommation, Decimal("3.33"))
        self.assertIsInstance(consommation, Decimal)

    # --- Cas refusés ---------------------------------------------------------

    def test_refus_litres_nuls(self):
        with self.assertRaises(ValidationError) as contexte:
            self._enregistrer(litres=Decimal("0"))

        self.assertIn("strictement positif", " ".join(contexte.exception.messages))
        self.assertEqual(PleinCarburant.objects.count(), 0)

    def test_refus_litres_negatifs(self):
        with self.assertRaises(ValidationError):
            self._enregistrer(litres=Decimal("-10"))

    def test_refus_prix_nul(self):
        with self.assertRaises(ValidationError):
            self._enregistrer(prix_litre=Decimal("0"))

    def test_refus_date_future(self):
        with self.assertRaises(ValidationError):
            self._enregistrer(date=self.aujourdhui + timedelta(days=1))

    def test_refus_compteur_inferieur_au_plein_precedent(self):
        self._enregistrer(date=self.aujourdhui - timedelta(days=5), km_compteur=100_400)

        with self.assertRaises(ValidationError) as contexte:
            self._enregistrer(km_compteur=100_200)

        self.assertIn("compteur", " ".join(contexte.exception.messages))
        self.assertEqual(PleinCarburant.objects.count(), 1)

    def test_refus_saisie_anterieure_au_dernier_plein(self):
        """On ne glisse pas un plein au milieu de l'historique."""
        self._enregistrer(date=self.aujourdhui, km_compteur=100_400)

        with self.assertRaises(ValidationError) as contexte:
            self._enregistrer(
                date=self.aujourdhui - timedelta(days=3), km_compteur=100_600
            )

        self.assertIn("ordre chronologique", " ".join(contexte.exception.messages))


class ConsommationTest(TestCase):
    """calculer_consommation et annoter_consommations."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(kilometrage=50_000)
        self.premier = PleinCarburant.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui - timedelta(days=20),
            litres=Decimal("40"),
            prix_litre=Decimal("700"),
            km_compteur=50_000,
        )
        self.second = PleinCarburant.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui - timedelta(days=5),
            litres=Decimal("45"),
            prix_litre=Decimal("715"),
            km_compteur=50_500,
        )

    def test_calculer_consommation(self):
        self.assertIsNone(services.calculer_consommation(self.premier))
        self.assertEqual(services.calculer_consommation(self.second), Decimal("9.00"))

    def test_un_autre_vehicule_ne_sert_pas_de_reference(self):
        """La consommation se calcule par véhicule, jamais entre deux véhicules."""
        autre = creer_vehicule(immatriculation="AA 0002 RB", kilometrage=9_000)
        plein = PleinCarburant.objects.create(
            vehicule=autre,
            chauffeur=self.chauffeur,
            date=self.aujourdhui,
            litres=Decimal("30"),
            prix_litre=Decimal("700"),
            km_compteur=9_200,
        )
        self.assertIsNone(services.calculer_consommation(plein))

    def test_annoter_consommations_en_une_seule_requete(self):
        """Le remède au N+1 : une requête, quel que soit le nombre de lignes."""
        pleins = list(PleinCarburant.objects.all())

        with self.assertNumQueries(1):
            annotes = services.annoter_consommations(pleins)

        par_pk = {plein.pk: plein.consommation for plein in annotes}
        self.assertIsNone(par_pk[self.premier.pk])
        self.assertEqual(par_pk[self.second.pk], Decimal("9.00"))

    def test_annoter_une_liste_vide(self):
        self.assertEqual(services.annoter_consommations([]), [])

    def test_compteur_incoherent_ne_donne_pas_de_nombre_absurde(self):
        """Données douteuses (import, admin) : None plutôt qu'un mensonge."""
        recul = PleinCarburant.objects.create(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui,
            litres=Decimal("30"),
            prix_litre=Decimal("700"),
            km_compteur=50_100,
        )
        self.assertIsNone(services.calculer_consommation(recul))


class EntretienTest(TestCase):
    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule(kilometrage=80_000)

    def _enregistrer(self, **champs):
        valeurs = {
            "vehicule": self.vehicule,
            "type_entretien": Entretien.TypeEntretien.VIDANGE,
            "date": self.aujourdhui,
            "km": 80_000,
            "cout": Decimal("35000"),
            "prestataire": "Garage Sodji — Akpakpa",
        }
        valeurs.update(champs)
        return services.enregistrer_entretien(**valeurs)

    # --- Cas nominal ---------------------------------------------------------

    def test_vidange_planifie_la_prochaine_echeance(self):
        entretien = self._enregistrer()
        self.assertEqual(entretien.prochaine_echeance_km, 85_000)

    def test_intervention_sans_echeance_kilometrique(self):
        entretien = self._enregistrer(type_entretien=Entretien.TypeEntretien.AUTRE)
        self.assertIsNone(entretien.prochaine_echeance_km)

    def test_echeance_explicite_prime_sur_la_regle(self):
        """Le garagiste annonce 4 000 km : sa valeur gagne sur l'intervalle."""
        entretien = self._enregistrer(prochaine_echeance_km=84_000)
        self.assertEqual(entretien.prochaine_echeance_km, 84_000)

    def test_le_releve_met_le_compteur_a_jour(self):
        self._enregistrer(km=82_000)
        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.kilometrage, 82_000)

    # --- Cas refusés ---------------------------------------------------------

    def test_refus_cout_negatif(self):
        with self.assertRaises(ValidationError):
            self._enregistrer(cout=Decimal("-1"))

        self.assertEqual(Entretien.objects.count(), 0)

    def test_refus_date_future(self):
        with self.assertRaises(ValidationError):
            self._enregistrer(date=self.aujourdhui + timedelta(days=1))

    def test_refus_echeance_anterieure_au_releve(self):
        with self.assertRaises(ValidationError) as contexte:
            self._enregistrer(km=80_000, prochaine_echeance_km=79_000)

        self.assertIn("échéance", " ".join(contexte.exception.messages))

    def test_refus_releve_anterieur_a_un_entretien_existant(self):
        self._enregistrer(km=80_000)

        with self.assertRaises(ValidationError) as contexte:
            self._enregistrer(
                km=70_000, type_entretien=Entretien.TypeEntretien.FREINS
            )

        self.assertIn("kilométrage", " ".join(contexte.exception.messages))
        self.assertEqual(Entretien.objects.count(), 1)


class StatutProchainEntretienTest(TestCase):
    def setUp(self):
        self.vehicule = creer_vehicule(kilometrage=100_000)
        self.entretien = Entretien.objects.create(
            vehicule=self.vehicule,
            type_entretien=Entretien.TypeEntretien.VIDANGE,
            date=timezone.localdate() - timedelta(days=30),
            km=95_000,
            cout=Decimal("30000"),
            prestataire="Garage Tokpa Auto",
            prochaine_echeance_km=100_000,
        )

    def test_entretien_en_retard(self):
        self.entretien.prochaine_echeance_km = 99_000
        statut = services.statut_prochain_entretien(self.entretien, 100_000)
        self.assertEqual(statut.code, services.ECHEANCE_EXPIREE)
        self.assertEqual(statut.km_restants, -1_000)
        self.assertTrue(statut.est_alerte)

    def test_entretien_proche(self):
        self.entretien.prochaine_echeance_km = 100_200
        statut = services.statut_prochain_entretien(self.entretien, 100_000)
        self.assertEqual(statut.code, services.ECHEANCE_BIENTOT)

    def test_entretien_a_venir(self):
        self.entretien.prochaine_echeance_km = 105_000
        statut = services.statut_prochain_entretien(self.entretien, 100_000)
        self.assertEqual(statut.code, services.ECHEANCE_VALIDE)
        self.assertFalse(statut.est_alerte)

    def test_entretien_sans_echeance(self):
        self.entretien.prochaine_echeance_km = None
        statut = services.statut_prochain_entretien(self.entretien, 100_000)
        self.assertEqual(statut.code, services.ECHEANCE_SANS)
        self.assertIsNone(statut.km_restants)


class StatutAssuranceTest(TestCase):
    """statut_assurance distingue « absente » de « sans échéance »."""

    def setUp(self):
        self.vehicule = creer_vehicule()

    def test_sans_attestation(self):
        statut = services.statut_assurance(self.vehicule)
        self.assertEqual(statut.code, services.ECHEANCE_ABSENTE)
        # Une absence d'attestation est une alerte, pas un état neutre. Elle
        # s'affiche en ambre : il y a une action à mener, mais ce n'est pas une
        # échéance dépassée.
        self.assertTrue(statut.est_alerte)
        self.assertEqual(statut.ton, "ambre")

    def test_avec_attestation_valide(self):
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=200)
        statut = services.statut_assurance(self.vehicule)
        self.assertEqual(statut.code, services.ECHEANCE_VALIDE)

    def test_visite_technique_absente(self):
        statut = services.statut_visite_technique(self.vehicule)
        self.assertEqual(statut.code, services.ECHEANCE_ABSENTE)
        self.assertEqual(statut.libelle, "Aucun procès-verbal")


class ActivationTest(TestCase):
    """Sortie et retour dans la flotte : le « soft delete » de FleetFlow."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def test_sortie_de_flotte(self):
        vehicule = services.basculer_activation_vehicule(self.vehicule)
        self.assertFalse(vehicule.actif)
        # Un véhicule sorti ne doit pas rester « disponible ».
        self.assertEqual(vehicule.statut, Vehicule.Statut.HORS_SERVICE)

    def test_reintegration(self):
        services.basculer_activation_vehicule(self.vehicule)
        vehicule = services.basculer_activation_vehicule(self.vehicule)
        self.assertTrue(vehicule.actif)
        self.assertEqual(vehicule.statut, Vehicule.Statut.DISPONIBLE)

    def test_refus_de_sortir_un_vehicule_en_mission(self):
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        with self.assertRaises(ValidationError) as contexte:
            services.basculer_activation_vehicule(self.vehicule)

        self.assertIn("en mission", " ".join(contexte.exception.messages))
        self.vehicule.refresh_from_db()
        self.assertTrue(self.vehicule.actif)

    def test_desactivation_du_chauffeur(self):
        chauffeur = services.basculer_activation_chauffeur(self.chauffeur)
        self.assertFalse(chauffeur.actif)

    def test_refus_de_desactiver_un_chauffeur_en_mission(self):
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Lomé",
            date_depart=self.aujourdhui,
        )
        with self.assertRaises(ValidationError):
            services.basculer_activation_chauffeur(self.chauffeur)

    def test_un_vehicule_sorti_ne_recoit_plus_de_mission(self):
        """Le lien entre la désactivation et le refus d'affectation."""
        services.basculer_activation_vehicule(self.vehicule)
        with self.assertRaises(ValidationError):
            services.creer_mission(
                vehicule=self.vehicule,
                chauffeur=self.chauffeur,
                depart="Cotonou",
                destination="Parakou",
                date_depart=self.aujourdhui,
            )


class IndicateursFlotteTest(TestCase):
    """Les quatre chiffres affichés sous la liste des camions."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.chauffeur = creer_chauffeur()
        self.camion = creer_vehicule(
            immatriculation="AB 1234 RB",
            annee=2016,
            kilometrage=400_000,
            type_vehicule=Vehicule.TypeVehicule.CAMION,
        )
        self.utilitaire = creer_vehicule(
            immatriculation="AC 4821 RB", annee=2020, kilometrage=100_000
        )

    def _plein(self, vehicule, jours, km, litres):
        return PleinCarburant.objects.create(
            vehicule=vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui - timedelta(days=jours),
            litres=Decimal(litres),
            prix_litre=Decimal("715"),
            km_compteur=km,
        )

    def test_kilometrage_cumule_et_effectif(self):
        indicateurs = services.indicateurs_flotte()
        self.assertEqual(indicateurs.total, 2)
        self.assertEqual(indicateurs.en_service, 2)
        self.assertEqual(indicateurs.kilometrage_cumule, 500_000)

    def test_consommation_ponderee_par_la_distance(self):
        """Le gros rouleur doit peser plus lourd que le petit.

        Camion : 320 L pour 1 000 km (32 L/100). Utilitaire : 20 L pour
        200 km (10 L/100). La moyenne *arithmétique* donnerait 21,0. La
        moyenne pondérée, seule juste, donne 340 L / 1 200 km = 28,3.
        """
        self._plein(self.camion, 30, 400_000, 300)
        self._plein(self.camion, 10, 401_000, 320)
        self._plein(self.utilitaire, 30, 100_000, 18)
        self._plein(self.utilitaire, 10, 100_200, 20)

        indicateurs = services.indicateurs_flotte()
        self.assertEqual(indicateurs.consommation_moyenne, Decimal("28.3"))

    def test_consommation_inconnue_sans_deux_pleins(self):
        """Un seul plein par camion : aucune distance mesurable."""
        self._plein(self.camion, 10, 400_000, 300)
        indicateurs = services.indicateurs_flotte()
        self.assertIsNone(indicateurs.consommation_moyenne)

    def test_age_moyen_et_plus_ancien(self):
        annee = self.aujourdhui.year
        attendu = Decimal(((annee - 2016) + (annee - 2020))) / Decimal(2)
        indicateurs = services.indicateurs_flotte()
        self.assertEqual(
            indicateurs.age_moyen, attendu.quantize(Decimal("0.1"))
        )
        self.assertEqual(indicateurs.annee_plus_ancien, 2016)

    def test_immobilisation(self):
        self.camion.statut = Vehicule.Statut.EN_MAINTENANCE
        self.camion.save(update_fields=["statut"])

        indicateurs = services.indicateurs_flotte()
        self.assertEqual(indicateurs.immobilises, ("AB 1234 RB",))
        # Un camion en maintenance n'est plus « en service ».
        self.assertEqual(indicateurs.en_service, 1)

    def test_les_camions_sortis_sont_exclus(self):
        """La flotte, c'est ce qui est actif : le reste est de l'historique."""
        creer_vehicule(immatriculation="AD 0000 RB", actif=False, kilometrage=900_000)
        indicateurs = services.indicateurs_flotte()
        self.assertEqual(indicateurs.total, 2)
        self.assertEqual(indicateurs.kilometrage_cumule, 500_000)

    def test_flotte_vide(self):
        Vehicule.objects.all().delete()
        indicateurs = services.indicateurs_flotte()
        self.assertEqual(indicateurs.total, 0)
        self.assertEqual(indicateurs.kilometrage_cumule, 0)
        self.assertIsNone(indicateurs.consommation_moyenne)
        self.assertIsNone(indicateurs.age_moyen)

    def test_deux_requetes_quelle_que_soit_la_taille(self):
        for index in range(6):
            creer_vehicule(immatriculation=f"ZZ {1000 + index} RB")
        with self.assertNumQueries(2):
            services.indicateurs_flotte()


class AnnotationsDeListeTest(TestCase):
    """Les annotations qui évitent une requête par ligne de tableau."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(kilometrage=50_000)

    def test_consommation_moyenne_par_vehicule(self):
        for jours, km, litres in ((20, 50_000, "40"), (5, 50_500, "45")):
            PleinCarburant.objects.create(
                vehicule=self.vehicule,
                chauffeur=self.chauffeur,
                date=self.aujourdhui - timedelta(days=jours),
                litres=Decimal(litres),
                prix_litre=Decimal("700"),
                km_compteur=km,
            )
        vehicules = services.annoter_consommation_moyenne([self.vehicule])
        # 45 L pour 500 km.
        self.assertEqual(vehicules[0].consommation_moyenne, Decimal("9.0"))

    def test_consommation_inconnue_et_non_nulle(self):
        vehicules = services.annoter_consommation_moyenne([self.vehicule])
        self.assertIsNone(vehicules[0].consommation_moyenne)

    def test_annoter_une_liste_vide(self):
        self.assertEqual(services.annoter_consommation_moyenne([]), [])

    def test_missions_en_cours_par_vehicule_en_une_requete(self):
        mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        autres = [creer_vehicule(immatriculation=f"ZZ {2000 + i} RB") for i in range(5)]

        with self.assertNumQueries(1):
            par_vehicule = services.missions_en_cours_par_vehicule(
                [self.vehicule] + autres
            )

        self.assertEqual(par_vehicule[self.vehicule.pk], mission)
        self.assertNotIn(autres[0].pk, par_vehicule)

    def test_missions_en_cours_par_chauffeur_en_une_requete(self):
        mission = services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Lomé",
            date_depart=self.aujourdhui,
        )
        with self.assertNumQueries(1):
            par_chauffeur = services.missions_en_cours_par_chauffeur([self.chauffeur])
        self.assertEqual(par_chauffeur[self.chauffeur.pk], mission)


class StatutsAffichablesTest(TestCase):
    """La traduction d'un état en ton de pastille, décidée dans les services."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule()
        self.chauffeur = creer_chauffeur()

    def test_tons_du_statut_operationnel(self):
        attendus = {
            Vehicule.Statut.EN_MISSION: "vert",
            Vehicule.Statut.DISPONIBLE: "neutre",
            Vehicule.Statut.EN_MAINTENANCE: "ambre",
            Vehicule.Statut.HORS_SERVICE: "rouge",
        }
        for statut, ton in attendus.items():
            with self.subTest(statut=statut):
                self.vehicule.statut = statut
                self.assertEqual(
                    services.statut_operationnel(self.vehicule).ton, ton
                )

    def test_un_camion_sorti_de_la_flotte_prime_sur_son_statut(self):
        """Inutile de savoir qu'il est « disponible » s'il n'est plus là."""
        self.vehicule.actif = False
        self.vehicule.statut = Vehicule.Statut.DISPONIBLE
        statut = services.statut_operationnel(self.vehicule)
        self.assertEqual(statut.ton, "rouge")
        self.assertEqual(statut.libelle, "Sorti de la flotte")

    def test_statut_chauffeur_en_mission(self):
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        statut = services.statut_chauffeur(self.chauffeur)
        self.assertEqual(statut.code, "en_mission")
        self.assertEqual(statut.ton, "vert")

    def test_statut_chauffeur_disponible(self):
        self.assertEqual(services.statut_chauffeur(self.chauffeur).ton, "neutre")

    def test_statut_chauffeur_desactive(self):
        self.chauffeur.actif = False
        statut = services.statut_chauffeur(self.chauffeur)
        self.assertEqual(statut.libelle, "Désactivé")
        self.assertEqual(statut.ton, "rouge")

    def test_statut_chauffeur_sans_requete_si_mission_fournie(self):
        """Passer None explicitement signifie « j'ai vérifié, il n'y en a pas ».

        Ce test a révélé une ambiguïté de signature : avec un défaut à None,
        le service ne pouvait pas distinguer « pas fourni » de « pas de
        mission », et interrogeait la base dans les deux cas. D'où la
        sentinelle _NON_FOURNI.
        """
        with self.assertNumQueries(0):
            statut = services.statut_chauffeur(self.chauffeur, mission_en_cours=None)
        self.assertEqual(statut.code, "disponible")

    def test_statut_chauffeur_interroge_si_rien_n_est_fourni(self):
        with self.assertNumQueries(1):
            services.statut_chauffeur(self.chauffeur)

    def test_tons_du_statut_mission(self):
        attendus = {
            Mission.Statut.EN_COURS: "vert",
            Mission.Statut.PLANIFIEE: "ambre",
            Mission.Statut.TERMINEE: "neutre",
            Mission.Statut.ANNULEE: "rouge",
        }
        mission = Mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Bohicon",
            date_depart=self.aujourdhui,
            km_depart=1_000,
        )
        for statut, ton in attendus.items():
            with self.subTest(statut=statut):
                mission.statut = statut
                self.assertEqual(services.statut_mission(mission).ton, ton)
