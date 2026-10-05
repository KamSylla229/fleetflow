"""Tests de la collecte et du classement des échéances datées.

Le classement est une fonction pure : ses tests héritent de SimpleTestCase,
ne créent aucune base, et construisent leurs échéances à la main avec des
dates choisies. C'est la raison d'être de la coupure entre les deux
fonctions — les règles se testent sans fixture.

La collecte, elle, lit la base : ses tests ont besoin d'un TestCase.
"""

from datetime import date, timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from fleet.echeances import (
    ASSURANCE,
    PERMIS,
    SEUIL_LOINTAIN_JOURS,
    SEUIL_PROCHE_JOURS,
    VISITE_TECHNIQUE,
    Echeance,
    classer_echeances,
    collecter_echeances,
)
from fleet.models import Document, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule

AUJOURDHUI = date(2026, 10, 5)


def echeance(jours, kind=ASSURANCE):
    """Une échéance à `jours` jours d'AUJOURDHUI, sans toucher la base."""
    return Echeance(kind=kind, date_echeance=AUJOURDHUI + timedelta(days=jours))


class ClasserEcheancesTest(SimpleTestCase):
    """La fonction pure : les bornes, le tri, et la règle du lendemain."""

    def test_liste_vide(self):
        classement = classer_echeances([], today=AUJOURDHUI)
        self.assertEqual(classement["depassees"], [])
        self.assertEqual(classement["sous_30_j"], [])
        self.assertEqual(classement["sous_60_j"], [])

    def test_une_echeance_dans_chaque_classe(self):
        classement = classer_echeances(
            [echeance(-5), echeance(10), echeance(45), echeance(200)],
            today=AUJOURDHUI,
        )
        self.assertEqual(len(classement["depassees"]), 1)
        self.assertEqual(len(classement["sous_30_j"]), 1)
        self.assertEqual(len(classement["sous_60_j"]), 1)

    def test_au_dela_de_soixante_jours_rien_n_est_classe(self):
        """Ce dont il n'y a rien à faire aujourd'hui n'encombre pas l'écran."""
        classement = classer_echeances([echeance(61), echeance(400)], today=AUJOURDHUI)
        self.assertEqual(classement["depassees"], [])
        self.assertEqual(classement["sous_30_j"], [])
        self.assertEqual(classement["sous_60_j"], [])

    def test_une_echeance_du_jour_n_est_pas_depassee(self):
        """La règle du cahier des charges : dépassée **le lendemain**.

        Une assurance qui expire aujourd'hui couvre encore la journée.
        """
        classement = classer_echeances([echeance(0)], today=AUJOURDHUI)
        self.assertEqual(classement["depassees"], [])
        self.assertEqual(len(classement["sous_30_j"]), 1)
        self.assertEqual(classement["sous_30_j"][0].jours, 0)
        self.assertFalse(classement["sous_30_j"][0].depassee)

    def test_le_lendemain_elle_est_depassee(self):
        classement = classer_echeances([echeance(-1)], today=AUJOURDHUI)
        self.assertEqual(len(classement["depassees"]), 1)
        self.assertEqual(classement["depassees"][0].jours, -1)
        self.assertTrue(classement["depassees"][0].depassee)

    def test_les_bornes_des_deux_seuils(self):
        """C'est là que se cachent les < écrits à la place des <=.

        30 jours est encore « proche » ; 31 passe en « lointain ». 60 est
        encore classé ; 61 ne l'est plus.
        """
        classement = classer_echeances(
            [
                echeance(SEUIL_PROCHE_JOURS),
                echeance(SEUIL_PROCHE_JOURS + 1),
                echeance(SEUIL_LOINTAIN_JOURS),
                echeance(SEUIL_LOINTAIN_JOURS + 1),
            ],
            today=AUJOURDHUI,
        )
        self.assertEqual(
            [item.jours for item in classement["sous_30_j"]], [SEUIL_PROCHE_JOURS]
        )
        self.assertEqual(
            [item.jours for item in classement["sous_60_j"]],
            [SEUIL_PROCHE_JOURS + 1, SEUIL_LOINTAIN_JOURS],
        )

    def test_tri_par_urgence(self):
        """La plus en retard d'abord, la plus proche d'abord."""
        classement = classer_echeances(
            [echeance(-2), echeance(-40), echeance(-17), echeance(25), echeance(3)],
            today=AUJOURDHUI,
        )
        self.assertEqual(
            [item.jours for item in classement["depassees"]], [-40, -17, -2]
        )
        self.assertEqual([item.jours for item in classement["sous_30_j"]], [3, 25])

    def test_le_kind_est_conserve(self):
        classement = classer_echeances(
            [echeance(5, kind=PERMIS), echeance(6, kind=VISITE_TECHNIQUE)],
            today=AUJOURDHUI,
        )
        self.assertEqual(
            [item.kind for item in classement["sous_30_j"]], [PERMIS, VISITE_TECHNIQUE]
        )

    def test_sans_today_la_fonction_prend_la_date_du_jour(self):
        """Le paramètre est facultatif ; l'appelant courant ne le fournit pas."""
        demain = timezone.localdate() + timedelta(days=1)
        classement = classer_echeances(
            [Echeance(kind=ASSURANCE, date_echeance=demain)]
        )
        self.assertEqual(len(classement["sous_30_j"]), 1)
        self.assertEqual(classement["sous_30_j"][0].jours, 1)

    def test_aucune_requete_n_est_faite(self):
        """La preuve que la fonction est pure : SimpleTestCase l'interdirait.

        SimpleTestCase refuse tout accès à la base de données ; que ce fichier
        entier en hérite est déjà la garantie. Ce test le dit explicitement
        pour que la propriété soit écrite et non seulement implicite.
        """
        classement = classer_echeances([echeance(-3), echeance(12)], today=AUJOURDHUI)
        self.assertEqual(len(classement["depassees"]) + len(classement["sous_30_j"]), 2)


class EcheanceTest(SimpleTestCase):
    """Les libellés, calculés sans requête depuis les objets déjà chargés."""

    def test_jours_avant(self):
        self.assertEqual(echeance(12).jours_avant(AUJOURDHUI), 12)
        self.assertEqual(echeance(-4).jours_avant(AUJOURDHUI), -4)

    def test_libelle_par_defaut(self):
        """Sans référence, un libellé neutre plutôt qu'une exception."""
        self.assertEqual(echeance(5).libelle, "Échéance")
        self.assertEqual(echeance(5).url, "")


class CollecterEcheancesTest(TestCase):
    """La collecte : lecture seule, et la règle du document en vigueur."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")

    def test_une_assurance_et_un_permis(self):
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=10)
        chauffeur = creer_chauffeur(
            date_expiration_permis=self.aujourdhui + timedelta(days=40)
        )

        echeances = collecter_echeances()

        kinds = sorted(item.kind for item in echeances)
        self.assertEqual(kinds, [ASSURANCE, PERMIS])

        assurance = next(item for item in echeances if item.kind == ASSURANCE)
        self.assertEqual(assurance.vehicule, self.vehicule)
        self.assertEqual(assurance.libelle, "Assurance — AB 1234 RB")
        self.assertEqual(assurance.url, self.vehicule.get_absolute_url())

        permis = next(item for item in echeances if item.kind == PERMIS)
        self.assertEqual(permis.chauffeur, chauffeur)
        self.assertIn("Permis —", permis.libelle)

    def test_seul_le_document_en_vigueur_est_collecte(self):
        """Le piège du renouvellement, côté lecture.

        Un camion réassuré porte deux lignes. Remonter l'ancienne afficherait
        une échéance dépassée pour l'éternité.
        """
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=-40)
        nouvelle = creer_document(
            self.vehicule, Document.TypeDocument.ASSURANCE, jours=320
        )

        assurances = [item for item in collecter_echeances() if item.kind == ASSURANCE]

        self.assertEqual(len(assurances), 1)
        self.assertEqual(assurances[0].document, nouvelle)

    def test_deux_types_differents_coexistent(self):
        """La règle porte sur le couple (camion, type), pas sur le camion."""
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=10)
        creer_document(self.vehicule, Document.TypeDocument.VISITE_TECHNIQUE, jours=20)

        kinds = sorted(item.kind for item in collecter_echeances())
        self.assertEqual(kinds, [ASSURANCE, VISITE_TECHNIQUE])

    def test_une_piece_sans_date_est_ignoree(self):
        """Une carte grise n'expire pas : elle n'a pas d'échéance."""
        creer_document(
            self.vehicule, Document.TypeDocument.CARTE_GRISE, date_expiration=None
        )
        self.assertEqual(collecter_echeances(), [])

    def test_une_licence_de_transport_est_collectee(self):
        """Toute pièce datée compte, pas seulement les trois nommées.

        Le seed crée des licences de transport avec une échéance : les ignorer
        laisserait passer un retard réel sans rien dire.
        """
        creer_document(
            self.vehicule, Document.TypeDocument.LICENCE_TRANSPORT, jours=15
        )
        echeances = collecter_echeances()
        self.assertEqual(len(echeances), 1)
        self.assertEqual(echeances[0].kind, "licence_transport")
        self.assertEqual(echeances[0].libelle, "Licence de transport — AB 1234 RB")

    def test_un_camion_sorti_de_la_flotte_est_ignore(self):
        sorti = creer_vehicule(immatriculation="ZZ 0001 RB", actif=False)
        creer_document(sorti, Document.TypeDocument.ASSURANCE, jours=-5)
        self.assertEqual(collecter_echeances(), [])

    def test_un_chauffeur_desactive_est_ignore(self):
        creer_chauffeur(
            nom="Ancien",
            permis="BJ-OLD",
            actif=False,
            date_expiration_permis=self.aujourdhui - timedelta(days=3),
        )
        self.assertEqual(collecter_echeances(), [])

    def test_un_camion_sans_attestation_ne_produit_rien(self):
        """Une pièce absente n'est pas une échéance.

        Un dossier vide est un autre problème — l'indicateur « assurance
        manquante », inscrit au BACKLOG —, pas un retard.
        """
        self.assertEqual(collecter_echeances(), [])

    def test_le_cout_ne_grossit_pas_avec_la_flotte(self):
        """Trois requêtes, grâce au prefetch_related et au tri en Python."""
        for index in range(6):
            vehicule = creer_vehicule(immatriculation=f"YY {4000 + index} RB")
            creer_document(vehicule, Document.TypeDocument.ASSURANCE, jours=20)
            creer_chauffeur(nom=f"Chauffeur {index}", permis=f"BJ-EC-{index}")

        with self.assertNumQueries(3):
            echeances = collecter_echeances()

        self.assertEqual(len(echeances), 12)

    def test_la_collecte_accepte_des_listes_fournies(self):
        """Pour qu'un appelant qui a déjà chargé la flotte ne la recharge pas."""
        creer_document(self.vehicule, Document.TypeDocument.ASSURANCE, jours=10)
        vehicules = list(Vehicule.objects.prefetch_related("documents"))

        with self.assertNumQueries(0):
            echeances = collecter_echeances(vehicules=vehicules, chauffeurs=[])

        self.assertEqual(len(echeances), 1)


class CollecteEtClassementTest(TestCase):
    """Les deux fonctions enchaînées, comme le fera la page Entretien."""

    def test_le_parcours_complet(self):
        aujourdhui = timezone.localdate()
        vehicule = creer_vehicule(immatriculation="AB 1234 RB")
        creer_document(vehicule, Document.TypeDocument.VISITE_TECHNIQUE, jours=-17)
        creer_document(vehicule, Document.TypeDocument.ASSURANCE, jours=24)
        creer_chauffeur(date_expiration_permis=aujourdhui + timedelta(days=47))

        classement = classer_echeances(collecter_echeances(), today=aujourdhui)

        self.assertEqual(len(classement["depassees"]), 1)
        self.assertEqual(classement["depassees"][0].jours, -17)
        self.assertEqual(classement["depassees"][0].libelle, "Visite technique — AB 1234 RB")
        self.assertEqual(len(classement["sous_30_j"]), 1)
        self.assertEqual(classement["sous_30_j"][0].jours, 24)
        self.assertEqual(len(classement["sous_60_j"]), 1)
        self.assertEqual(classement["sous_60_j"][0].jours, 47)
