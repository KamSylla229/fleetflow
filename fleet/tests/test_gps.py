"""Tests du socle GPS : itinéraires, simulation, statut et commande.

Les tests d'itinéraires n'ont besoin d'aucune base de données — c'est tout
l'intérêt d'avoir gardé fleet/itineraires.py indépendant de Django. Ils
héritent quand même de SimpleTestCase pour rester lancés par le même
`manage.py test`.
"""

import random
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from fleet import services
from fleet.itineraires import (
    COTONOU_LOKOSSA,
    COTONOU_PARAKOU,
    COTONOU_PORTO_NOVO,
    ITINERAIRES,
    distance_km,
    itineraire_par_code,
)
from fleet.models import FournisseurGPS, PositionGPS, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_vehicule


class ItinerairesTest(SimpleTestCase):
    """Géométrie pure : aucune base, aucun modèle."""

    def test_point_de_depart_et_d_arrivee(self):
        for itineraire in ITINERAIRES.values():
            with self.subTest(itineraire=itineraire.code):
                self.assertEqual(itineraire.point_a(0), itineraire.points[0])
                self.assertEqual(itineraire.point_a(1), itineraire.points[-1])

    def test_longueur_cotonou_parakou(self):
        """La RNIE 2 fait environ 410 km ; on tolère 380 à 460.

        C'est la longueur *routière* : la polyligne ville à ville ne mesure que
        357 km, l'écart étant les virages entre les villes. Voir le coefficient
        de sinuosité dans fleet/itineraires.py.
        """
        self.assertGreater(COTONOU_PARAKOU.longueur_km, 380)
        self.assertLess(COTONOU_PARAKOU.longueur_km, 460)

    def test_la_longueur_routiere_depasse_la_polyligne(self):
        for itineraire in ITINERAIRES.values():
            with self.subTest(itineraire=itineraire.code):
                self.assertGreaterEqual(
                    itineraire.longueur_km, itineraire.longueur_polyligne_km
                )

    def test_longueurs_des_trajets_courts(self):
        # Cotonou - Porto-Novo : une trentaine de kilomètres.
        self.assertGreater(COTONOU_PORTO_NOVO.longueur_km, 30)
        self.assertLess(COTONOU_PORTO_NOVO.longueur_km, 55)
        # Cotonou - Lokossa : une centaine.
        self.assertGreater(COTONOU_LOKOSSA.longueur_km, 85)
        self.assertLess(COTONOU_LOKOSSA.longueur_km, 125)

    def test_la_progression_avance_vers_le_nord(self):
        """Sur Cotonou - Parakou, la latitude croît avec la progression."""
        latitudes = [COTONOU_PARAKOU.point_a(t / 10)[0] for t in range(11)]
        for precedente, suivante in zip(latitudes, latitudes[1:]):
            self.assertGreaterEqual(suivante, precedente)

    def test_le_milieu_est_a_mi_distance_reelle(self):
        """point_a(0,5) est à mi-chemin en kilomètres, pas à mi-liste.

        Les segments ont des longueurs très inégales — 10 km entre Cotonou et
        Abomey-Calavi, 95 km entre Savè et Tchaourou. Interpoler sur l'indice
        du point au lieu de la distance placerait le camion loin de sa vraie
        position.

        On mesure en échantillonnant finement la première moitié du tracé :
        la longueur de cette chaîne doit valoir la moitié de la polyligne.
        """
        pas = 0.001
        chaine = [COTONOU_PARAKOU.point_a(rang * pas) for rang in range(501)]
        mesure = sum(distance_km(a, b) for a, b in zip(chaine, chaine[1:]))

        self.assertAlmostEqual(
            mesure, COTONOU_PARAKOU.longueur_polyligne_km / 2, delta=1.0
        )

        # Et ce milieu n'est pas le sommet médian de la liste : si c'était le
        # cas, l'interpolation se ferait sur l'indice et non sur la distance.
        sommet_median = COTONOU_PARAKOU.points[len(COTONOU_PARAKOU.points) // 2]
        self.assertNotEqual(COTONOU_PARAKOU.point_a(0.5), sommet_median)

    def test_bornes_hors_itineraire(self):
        """Une progression hors de [0, 1] ne doit pas lever d'exception."""
        self.assertEqual(COTONOU_PARAKOU.point_a(-3), COTONOU_PARAKOU.points[0])
        self.assertEqual(COTONOU_PARAKOU.point_a(7), COTONOU_PARAKOU.points[-1])

    def test_code_inconnu_ou_vide(self):
        self.assertIsNone(itineraire_par_code(""))
        self.assertIsNone(itineraire_par_code(None))
        self.assertIsNone(itineraire_par_code("cotonou_tombouctou"))
        self.assertEqual(itineraire_par_code("cotonou_parakou"), COTONOU_PARAKOU)

    def test_distance_connue(self):
        """Cotonou - Porto-Novo à vol d'oiseau : environ 26 km."""
        cotonou = (6.3703, 2.3912)
        porto_novo = (6.4969, 2.6283)
        self.assertAlmostEqual(distance_km(cotonou, porto_novo), 29.7, delta=2.0)
        self.assertEqual(distance_km(cotonou, cotonou), 0)


class AvancerPositionTest(TestCase):
    """Le moteur de simulation : ce qui bouge, et ce qui ne bouge pas."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.fournisseur = FournisseurGPS.objects.create(nom="Cartrack")
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(
            itineraire="cotonou_parakou",
            vitesse_nominale_kmh=60,
            fournisseur_gps=self.fournisseur,
            boitier_id="CT-00001",
        )
        # rng fixe : sans lui, un test sur la distance parcourue réussirait ou
        # échouerait selon le tirage, ce qui est la définition d'un test
        # inutile.
        self.rng = random.Random(42)

    def _mettre_en_mission(self, vehicule=None):
        return services.creer_mission(
            vehicule=vehicule or self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )

    # --- Cas nominal ---------------------------------------------------------

    def test_un_camion_en_mission_avance(self):
        self._mettre_en_mission()
        position = services.avancer_position(self.vehicule, 300, rng=self.rng)

        self.assertIsNotNone(position)
        self.assertEqual(position.source, PositionGPS.Source.SIMULATION)
        self.vehicule.refresh_from_db()
        self.assertGreater(self.vehicule.progression, 0)

    def test_la_distance_correspond_a_la_vitesse(self):
        """Cinq minutes à environ 60 km/h : de l'ordre de 5 km, soit ~1,25 %.

        La vitesse est tirée à ±15 % de la nominale, donc la borne est large ;
        ce que le test vérifie, c'est l'ordre de grandeur et l'unité — une
        erreur de conversion heures/secondes se verrait immédiatement.
        """
        self._mettre_en_mission()
        services.avancer_position(self.vehicule, 300, rng=self.rng)
        self.vehicule.refresh_from_db()

        parcouru_km = self.vehicule.progression * COTONOU_PARAKOU.longueur_km
        self.assertGreater(parcouru_km, 4)
        self.assertLess(parcouru_km, 6)

    def test_le_fournisseur_note_son_dernier_echange(self):
        self._mettre_en_mission()
        self.assertIsNone(self.fournisseur.dernier_echange)

        position = services.avancer_position(self.vehicule, 300, rng=self.rng)

        self.fournisseur.refresh_from_db()
        self.assertEqual(self.fournisseur.dernier_echange, position.horodatage)

    def test_un_camion_sans_fournisseur_avance_quand_meme(self):
        """Pas de boîtier déclaré n'empêche pas la simulation de tourner."""
        vehicule = creer_vehicule(
            immatriculation="ZZ 0001 RB", itineraire="cotonou_lokossa"
        )
        self._mettre_en_mission(vehicule=vehicule)
        self.assertIsNotNone(services.avancer_position(vehicule, 300, rng=self.rng))

    # --- Cas où rien ne doit bouger -----------------------------------------

    def test_un_camion_sans_mission_n_avance_pas(self):
        """Un camion au dépôt ne produit pas de trajet."""
        self.assertIsNone(services.avancer_position(self.vehicule, 300, rng=self.rng))
        self.assertEqual(PositionGPS.objects.count(), 0)
        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.progression, 0)

    def test_un_signal_coupe_n_ecrit_rien(self):
        """L'interrupteur de démonstration : le boîtier se tait."""
        self._mettre_en_mission()
        services.basculer_signal(self.vehicule, coupe=True)

        self.assertIsNone(services.avancer_position(self.vehicule, 300, rng=self.rng))
        self.assertEqual(PositionGPS.objects.count(), 0)
        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.progression, 0)

    def test_un_camion_sans_itineraire_n_avance_pas(self):
        vehicule = creer_vehicule(immatriculation="ZZ 0002 RB", itineraire="")
        self._mettre_en_mission(vehicule=vehicule)
        self.assertIsNone(services.avancer_position(vehicule, 300, rng=self.rng))
        self.assertEqual(PositionGPS.objects.count(), 0)

    def test_duree_nulle_ou_negative_refusee(self):
        self._mettre_en_mission()
        for duree in (0, -60):
            with self.subTest(duree=duree):
                with self.assertRaises(ValidationError):
                    services.avancer_position(self.vehicule, duree, rng=self.rng)

    # --- Aller-retour --------------------------------------------------------

    def test_demi_tour_a_l_arrivee(self):
        self._mettre_en_mission()
        self.vehicule.progression = 0.999
        self.vehicule.save(update_fields=["progression"])

        services.avancer_position(self.vehicule, 3600, rng=self.rng)

        self.vehicule.refresh_from_db()
        # La progression est bornée à 1 : sans la borne, un tick long
        # l'enverrait à 1,04 et le marqueur hors du tracé.
        self.assertEqual(self.vehicule.progression, 1.0)
        self.assertEqual(self.vehicule.direction, -1)

    def test_demi_tour_au_retour(self):
        self._mettre_en_mission()
        self.vehicule.progression = 0.001
        self.vehicule.direction = -1
        self.vehicule.save(update_fields=["progression", "direction"])

        services.avancer_position(self.vehicule, 3600, rng=self.rng)

        self.vehicule.refresh_from_db()
        self.assertEqual(self.vehicule.progression, 0.0)
        self.assertEqual(self.vehicule.direction, 1)

    def test_le_retour_fait_reculer_la_progression(self):
        self._mettre_en_mission()
        self.vehicule.progression = 0.5
        self.vehicule.direction = -1
        self.vehicule.save(update_fields=["progression", "direction"])

        services.avancer_position(self.vehicule, 300, rng=self.rng)

        self.vehicule.refresh_from_db()
        self.assertLess(self.vehicule.progression, 0.5)
        self.assertEqual(self.vehicule.direction, -1)

    def test_la_simulation_produit_parfois_des_arrets(self):
        """Une simulation trop parfaite ne teste rien.

        Avec une chance sur vingt par relevé, deux cents relevés donnent un
        arrêt avec une quasi-certitude. Le rng est fixe, donc le résultat est
        reproductible.
        """
        self._mettre_en_mission()
        for _ in range(200):
            services.avancer_position(self.vehicule, 60, rng=self.rng)

        vitesses = set(PositionGPS.objects.values_list("vitesse_kmh", flat=True))
        self.assertIn(Decimal("0.0"), vitesses)
        self.assertGreater(len(vitesses), 1)

    def test_le_tick_de_flotte_ne_touche_que_les_camions_concernes(self):
        self._mettre_en_mission()
        creer_vehicule(immatriculation="ZZ 0003 RB", itineraire="cotonou_lokossa")

        resultats = services.avancer_flotte(300, rng=self.rng)

        deplaces = [v for v, p in resultats if p is not None]
        self.assertEqual([v.pk for v in deplaces], [self.vehicule.pk])


class StatutGPSTest(TestCase):
    """Les quatre états, et la borne exacte du seuil."""

    def setUp(self):
        self.maintenant = timezone.now()
        self.vehicule = creer_vehicule()

    def _position(self, minutes, vitesse="60.0"):
        return PositionGPS.objects.create(
            vehicule=self.vehicule,
            latitude=Decimal("6.370300"),
            longitude=Decimal("2.391200"),
            vitesse_kmh=Decimal(vitesse),
            horodatage=self.maintenant - timedelta(minutes=minutes),
        )

    def test_aucune_donnee(self):
        """Jamais rien reçu : ce n'est pas une panne, c'est un camion non équipé."""
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_AUCUNE_DONNEE)
        self.assertEqual(statut.ton, "neutre")

    def test_en_route(self):
        self._position(minutes=2, vitesse="62.5")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_EN_ROUTE)
        self.assertEqual(statut.ton, "vert")
        self.assertEqual(statut.libelle, "En route")

    def test_a_arret(self):
        self._position(minutes=2, vitesse="0")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_A_ARRET)
        self.assertEqual(statut.ton, "ambre")

    @override_settings(FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=30)
    def test_sans_signal(self):
        self._position(minutes=220, vitesse="0")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_SANS_SIGNAL)
        self.assertEqual(statut.ton, "rouge")

    @override_settings(FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=30)
    def test_la_borne_exacte_du_seuil(self):
        """Pile à l'âge du seuil, le relevé est encore considéré comme reçu.

        La comparaison est stricte (`>` et non `>=`) : la borne doit être
        décidée et testée, pas subie. C'est là que se cachent les erreurs.
        """
        position = self._position(minutes=30, vitesse="0")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_A_ARRET)

        # Une seconde de plus, et le boîtier est déclaré muet.
        position.horodatage -= timedelta(seconds=1)
        position.save(update_fields=["horodatage"])
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_SANS_SIGNAL)

    @override_settings(FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=1)
    def test_le_seuil_vient_des_reglages(self):
        """La démonstration abaisse le seuil à une minute dans le .env."""
        self._position(minutes=2, vitesse="0")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_SANS_SIGNAL)

    def test_seul_le_dernier_releve_compte(self):
        self._position(minutes=200, vitesse="0")
        self._position(minutes=1, vitesse="55")
        statut = services.statut_gps(self.vehicule, maintenant=self.maintenant)
        self.assertEqual(statut.code, services.GPS_EN_ROUTE)

    def test_derniere_position(self):
        self._position(minutes=90)
        recente = self._position(minutes=1)
        self.assertEqual(services.derniere_position(self.vehicule), recente)


class AnnotationGPSTest(TestCase):
    """Le statut GPS d'une liste ne doit pas coûter une requête par ligne."""

    def setUp(self):
        self.maintenant = timezone.now()
        for index in range(6):
            vehicule = creer_vehicule(immatriculation=f"ZZ {1000 + index} RB")
            PositionGPS.objects.create(
                vehicule=vehicule,
                latitude=Decimal("6.370300"),
                longitude=Decimal("2.391200"),
                vitesse_kmh=Decimal("40") if index % 2 else Decimal("0"),
                horodatage=self.maintenant - timedelta(minutes=index),
            )

    def test_une_seule_requete_pour_toute_la_liste(self):
        queryset = services.annoter_dernieres_positions(Vehicule.objects.all())
        with self.assertNumQueries(1):
            vehicules = list(queryset)
            for vehicule in vehicules:
                services.statut_gps(vehicule, maintenant=self.maintenant)

        self.assertEqual(len(vehicules), 6)

    def test_les_valeurs_annotees_sont_justes(self):
        queryset = services.annoter_dernieres_positions(Vehicule.objects.all())
        par_plaque = {v.immatriculation: v for v in queryset}

        roulant = par_plaque["ZZ 1001 RB"]
        self.assertEqual(roulant.gps_vitesse_kmh, Decimal("40.0"))
        self.assertEqual(
            services.statut_gps(roulant, maintenant=self.maintenant).code,
            services.GPS_EN_ROUTE,
        )

        arrete = par_plaque["ZZ 1000 RB"]
        self.assertEqual(
            services.statut_gps(arrete, maintenant=self.maintenant).code,
            services.GPS_A_ARRET,
        )

    def test_la_liste_des_camions_ne_grossit_pas_en_requetes(self):
        """Six camions ne doivent pas coûter plus que deux.

        Le garde-fou qui compte : on compare deux mesures au lieu de figer un
        nombre, pour que le test n'échoue pas à la moindre évolution de la page.
        """
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.urls import reverse

        utilisateur = get_user_model().objects.create_user(username="gestionnaire")
        self.client.force_login(utilisateur)
        adresse = reverse("fleet:vehicule_liste")

        Vehicule.objects.exclude(
            immatriculation__in=["ZZ 1000 RB", "ZZ 1001 RB"]
        ).delete()
        with CaptureQueriesContext(connection) as avec_deux:
            self.client.get(adresse)

        for index in range(4):
            creer_vehicule(immatriculation=f"YY {2000 + index} RB")
        with CaptureQueriesContext(connection) as avec_six:
            self.client.get(adresse)

        self.assertEqual(len(avec_deux), len(avec_six))


class CommandeSimulerPositionsTest(TestCase):
    """La commande : un tick, la boucle mise à part, et les deux interrupteurs."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(
            immatriculation="AB 1234 RB", itineraire="cotonou_parakou"
        )

    def _appeler(self, *args):
        sortie = StringIO()
        call_command("simuler_positions", *args, stdout=sortie, stderr=sortie)
        return sortie.getvalue()

    def test_un_tick_sans_camion_a_deplacer(self):
        sortie = self._appeler()
        self.assertIn("0 position", sortie)
        self.assertIn("aucun camion a deplacer", sortie)
        self.assertEqual(PositionGPS.objects.count(), 0)

    def test_un_tick_deplace_le_camion_en_mission(self):
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        sortie = self._appeler()
        self.assertIn("AB 1234 RB", sortie)
        self.assertIn("progression", sortie)
        self.assertEqual(PositionGPS.objects.count(), 1)

    def test_la_sortie_est_en_ascii(self):
        """Rappel du bug du 30/09 : la console Windows écrit en cp1252.

        Une flèche ou un emoji y lève une UnicodeEncodeError, et comme la
        commande peut tourner dans une transaction, une erreur d'affichage
        détruirait le travail accompli.
        """
        services.creer_mission(
            vehicule=self.vehicule,
            chauffeur=self.chauffeur,
            depart="Cotonou",
            destination="Parakou",
            date_depart=self.aujourdhui,
        )
        sortie = self._appeler()
        sortie.encode("ascii")  # lève UnicodeEncodeError si un caractère sort

        # Les deux interrupteurs aussi : c'est là qu'un guillemet français
        # s'était glissé, et les guillemets « » ne sont pas de l'ASCII.
        self._appeler("--couper", "AB 1234 RB").encode("ascii")
        self._appeler("--retablir", "AB 1234 RB").encode("ascii")

    def test_couper_et_retablir_le_signal(self):
        sortie = self._appeler("--couper", "AB 1234 RB")
        self.assertIn("signal coupe", sortie)
        self.vehicule.refresh_from_db()
        self.assertTrue(self.vehicule.signal_coupe)

        sortie = self._appeler("--retablir", "AB 1234 RB")
        self.assertIn("signal retabli", sortie)
        self.vehicule.refresh_from_db()
        self.assertFalse(self.vehicule.signal_coupe)

    def test_plaque_inconnue(self):
        with self.assertRaises(CommandError) as contexte:
            self._appeler("--couper", "XX 0000 RB")
        self.assertIn("Aucun camion", str(contexte.exception))

    def test_intervalle_invalide(self):
        with self.assertRaises(CommandError):
            self._appeler("--intervalle", "0")
