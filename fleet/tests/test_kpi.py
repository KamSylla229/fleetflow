"""Tests des indicateurs du tableau de bord.

Trois choses y sont éprouvées, dans cet ordre d'importance :

1. la consommation **pondérée par la distance**, et son appariement de pleins
   sur une fenêtre de trente jours — le calcul le plus facile à se tromper ;
2. le fuseau horaire : les kilomètres du tableau de bord doivent être les
   mêmes que ceux du rapport quotidien, à la seconde près ;
3. le budget de requêtes, mesuré avec cinq puis vingt-six camions. S'il
   croît, c'est qu'un N+1 se cache quelque part.
"""

import datetime
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from fleet import services
from fleet.kpi import calculer_kpi
from fleet.models import Document, FournisseurGPS, PleinCarburant, PositionGPS, Vehicule
from fleet.tests.fabriques import creer_chauffeur, creer_document, creer_vehicule

# Deux points sur la RNIE 2, à une centaine de kilomètres l'un de l'autre.
COTONOU = (6.3703, 2.3912)
BOHICON = (7.1781, 2.0667)


class ConsommationPondereeTest(TestCase):
    """Σ litres ÷ Σ km × 100, sur les paires de pleins retenues."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")

    def _plein(self, vehicule, jours, km, litres):
        """Un plein il y a `jours` jours, au compteur `km`."""
        return PleinCarburant.objects.create(
            vehicule=vehicule,
            chauffeur=self.chauffeur,
            date=self.aujourdhui - timedelta(days=jours),
            litres=Decimal(str(litres)),
            prix_litre=Decimal("715"),
            km_compteur=km,
        )

    def test_le_plein_juste_avant_la_fenetre_sert_de_point_de_depart(self):
        """Le piège du point 8, et la raison de ne pas filtrer avant d'apparier.

        Le plein de J-40 est hors fenêtre, mais c'est lui qui donne le
        kilométrage de départ de la paire retenue. Filtrer les pleins avant
        l'appariement aurait compté les 300 litres de J-20 **sans** leurs
        1 000 km, et la consommation affichée aurait explosé.
        """
        self._plein(self.vehicule, jours=40, km=100_000, litres=280)
        self._plein(self.vehicule, jours=20, km=101_000, litres=300)

        depuis = self.aujourdhui - timedelta(days=30)
        consommation = services.consommation_moyenne_ponderee(
            [self.vehicule.pk], depuis=depuis
        )

        # 300 L pour 1 000 km = 30,0 L/100 km. Les 280 L du plein hors fenêtre
        # ne sont pas comptés : ils appartiennent à la période précédente.
        self.assertEqual(consommation, Decimal("30.0"))

    def test_les_paires_entierement_hors_fenetre_sont_ignorees(self):
        self._plein(self.vehicule, jours=90, km=100_000, litres=280)
        self._plein(self.vehicule, jours=80, km=101_000, litres=300)

        depuis = self.aujourdhui - timedelta(days=30)
        self.assertIsNone(
            services.consommation_moyenne_ponderee([self.vehicule.pk], depuis=depuis)
        )

    def test_la_ponderation_differe_de_la_moyenne_simple(self):
        """Un camion à 80 km ne doit pas peser autant qu'un camion à 3 000 km.

        Gros rouleur : 960 L pour 3 000 km, soit 32,0 L/100 km.
        Petit rouleur : 8 L pour 80 km, soit 10,0 L/100 km.

        Moyenne simple des deux : 21,0 — un chiffre que rien ne vérifie.
        Moyenne pondérée : 968 L / 3 080 km = 31,4 L/100 km, c'est-à-dire ce
        qu'on obtient en divisant la facture de carburant par les kilomètres
        du parc.
        """
        petit = creer_vehicule(immatriculation="AK 9255 RB")
        self._plein(self.vehicule, jours=20, km=100_000, litres=200)
        self._plein(self.vehicule, jours=5, km=103_000, litres=960)
        self._plein(petit, jours=20, km=20_000, litres=5)
        self._plein(petit, jours=5, km=20_080, litres=8)

        consommation = services.consommation_moyenne_ponderee(
            [self.vehicule.pk, petit.pk], depuis=self.aujourdhui - timedelta(days=30)
        )

        self.assertEqual(consommation, Decimal("31.4"))
        # La moyenne simple aurait donné 21,0 : le test échouerait si quelqu'un
        # « simplifiait » le calcul en moyennant les consommations.
        self.assertNotEqual(consommation, Decimal("21.0"))

    def test_aucun_plein_donne_none(self):
        self.assertIsNone(
            services.consommation_moyenne_ponderee([self.vehicule.pk])
        )

    def test_un_seul_plein_donne_none(self):
        """Sans paire, aucune distance : « on ne sait pas », pas zéro."""
        self._plein(self.vehicule, jours=5, km=100_000, litres=300)
        self.assertIsNone(
            services.consommation_moyenne_ponderee([self.vehicule.pk])
        )

    def test_un_compteur_immobile_donne_none_et_non_une_division_par_zero(self):
        """Deux pleins au même kilométrage : Σ km = 0."""
        self._plein(self.vehicule, jours=10, km=100_000, litres=300)
        self._plein(self.vehicule, jours=5, km=100_000, litres=300)
        self.assertIsNone(
            services.consommation_moyenne_ponderee([self.vehicule.pk])
        )

    def test_une_flotte_vide_donne_none(self):
        self.assertIsNone(services.consommation_moyenne_ponderee([]))

    def test_sans_fenetre_toutes_les_paires_comptent(self):
        """Les appelants existants ne changent pas de comportement."""
        self._plein(self.vehicule, jours=200, km=100_000, litres=280)
        self._plein(self.vehicule, jours=190, km=101_000, litres=300)
        self.assertEqual(
            services.consommation_moyenne_ponderee(
                [self.vehicule.pk], depuis=self.aujourdhui - timedelta(days=365)
            ),
            Decimal("30.0"),
        )


class CalculerKpiTest(TestCase):
    """Les cinq indicateurs, sur des données fabriquées à la main."""

    def setUp(self):
        self.aujourdhui = timezone.localdate()
        self.maintenant = timezone.now()
        self.fournisseur = FournisseurGPS.objects.create(nom="Cartrack")
        self.chauffeur = creer_chauffeur()

    def _camion(self, immatriculation, **champs):
        return creer_vehicule(
            immatriculation=immatriculation,
            fournisseur_gps=self.fournisseur,
            boitier_id="CT-1",
            **champs,
        )

    def _position(self, vehicule, point, minutes_avant=1, vitesse="60"):
        latitude, longitude = point
        return PositionGPS.objects.create(
            vehicule=vehicule,
            latitude=Decimal(str(latitude)),
            longitude=Decimal(str(longitude)),
            vitesse_kmh=Decimal(vitesse),
            horodatage=self.maintenant - timedelta(minutes=minutes_avant),
        )

    def test_en_service_exclut_maintenance_et_hors_service(self):
        self._camion("AB 1234 RB")
        self._camion("AC 4821 RB", statut=Vehicule.Statut.EN_MISSION)
        self._camion("AD 7734 RB", statut=Vehicule.Statut.EN_MAINTENANCE)
        self._camion("AE 2096 RB", statut=Vehicule.Statut.HORS_SERVICE)
        self._camion("AF 5517 RB", actif=False)

        kpi = calculer_kpi(self.aujourdhui)

        # Quatre camions actifs, dont deux en service.
        self.assertEqual(kpi["total_camions"], 4)
        self.assertEqual(kpi["en_service"], 2)
        self.assertEqual(kpi["en_maintenance"], 1)

    def test_en_mouvement_compte_les_camions_en_route(self):
        roulant = self._camion("AB 1234 RB")
        arrete = self._camion("AC 4821 RB")
        muet = self._camion("AD 7734 RB")
        self._camion("AE 2096 RB")  # aucun relevé

        self._position(roulant, COTONOU, minutes_avant=1, vitesse="62")
        self._position(arrete, COTONOU, minutes_avant=2, vitesse="0")
        self._position(muet, COTONOU, minutes_avant=300, vitesse="55")

        kpi = calculer_kpi(self.aujourdhui)

        self.assertEqual(kpi["en_mouvement"], 1)

    def test_km_du_jour_est_la_somme_par_camion(self):
        camion = self._camion("AB 1234 RB")
        self._position(camion, COTONOU, minutes_avant=60)
        self._position(camion, BOHICON, minutes_avant=10)

        kpi = calculer_kpi(self.aujourdhui)

        # Le même calcul que le rapport quotidien, par construction.
        attendu = services.kilometres_parcourus(self.aujourdhui)
        self.assertEqual(kpi["km_par_camion"], attendu)
        self.assertEqual(kpi["km_du_jour"], round(attendu[0][1], 1))
        self.assertGreater(kpi["km_du_jour"], 80)

    def test_km_du_jour_vaut_zero_sans_position(self):
        self._camion("AB 1234 RB")
        kpi = calculer_kpi(self.aujourdhui)
        self.assertEqual(kpi["km_du_jour"], 0)
        self.assertEqual(kpi["km_par_camion"], [])

    def test_consommation_none_sans_assez_de_donnees(self):
        """L'écran affichera un tiret, et jamais une division par zéro."""
        camion = self._camion("AB 1234 RB")
        PleinCarburant.objects.create(
            vehicule=camion,
            chauffeur=self.chauffeur,
            date=self.aujourdhui,
            litres=Decimal("300"),
            prix_litre=Decimal("715"),
            km_compteur=100_000,
        )
        self.assertIsNone(calculer_kpi(self.aujourdhui)["consommation_moy"])

    def test_consommation_calculee_sur_la_fenetre(self):
        camion = self._camion("AB 1234 RB")
        for jours, km, litres in ((20, 100_000, "200"), (5, 101_000, "320")):
            PleinCarburant.objects.create(
                vehicule=camion,
                chauffeur=self.chauffeur,
                date=self.aujourdhui - timedelta(days=jours),
                litres=Decimal(litres),
                prix_litre=Decimal("715"),
                km_compteur=km,
            )
        self.assertEqual(
            calculer_kpi(self.aujourdhui)["consommation_moy"], Decimal("32.0")
        )

    def test_echeances_60j_compte_les_trois_colonnes(self):
        camion = self._camion("AB 1234 RB")
        creer_document(camion, Document.TypeDocument.ASSURANCE, jours=-10)
        creer_document(camion, Document.TypeDocument.VISITE_TECHNIQUE, jours=20)
        creer_document(camion, Document.TypeDocument.LICENCE_TRANSPORT, jours=50)
        creer_document(camion, Document.TypeDocument.AUTRE, jours=300)
        creer_chauffeur(
            nom="Second", permis="BJ-2",
            date_expiration_permis=self.aujourdhui + timedelta(days=15),
        )

        kpi = calculer_kpi(self.aujourdhui)

        # Trois pièces dans la fenêtre, plus deux permis (celui du setUp est à
        # un an, donc hors fenêtre) : 3 + 1.
        self.assertEqual(kpi["echeances_60j"], 4)
        self.assertEqual(kpi["depassees"], 1)

    def test_une_flotte_vide_ne_leve_rien(self):
        kpi = calculer_kpi(self.aujourdhui)
        self.assertEqual(kpi["en_service"], 0)
        self.assertEqual(kpi["en_mouvement"], 0)
        self.assertEqual(kpi["km_du_jour"], 0)
        self.assertIsNone(kpi["consommation_moy"])
        self.assertEqual(kpi["echeances_60j"], 0)


@override_settings(TIME_ZONE="Africa/Porto-Novo", USE_TZ=True)
class FuseauHoraireTest(TestCase):
    """Les kilomètres du tableau de bord et du rapport doivent coïncider."""

    def setUp(self):
        self.chauffeur = creer_chauffeur()
        self.vehicule = creer_vehicule(immatriculation="AB 1234 RB")

    def _position_locale(self, jour, heure, minute, point):
        """Un relevé à une heure **locale** donnée.

        make_aware attache le fuseau du projet : 23 h 30 à Porto-Novo
        (UTC+1) est stocké comme 22 h 30 UTC. C'est tout l'enjeu du test —
        sans conversion, ce relevé basculerait dans le jour suivant.
        """
        latitude, longitude = point
        quand = timezone.make_aware(
            datetime.datetime.combine(jour, datetime.time(heure, minute)),
            timezone.get_current_timezone(),
        )
        return PositionGPS.objects.create(
            vehicule=self.vehicule,
            latitude=Decimal(str(latitude)),
            longitude=Decimal(str(longitude)),
            vitesse_kmh=Decimal("60"),
            horodatage=quand,
        )

    def test_un_releve_a_23h30_locale_appartient_au_bon_jour(self):
        jour = timezone.localdate()
        self._position_locale(jour, 23, 0, COTONOU)
        self._position_locale(jour, 23, 30, BOHICON)

        kpi = calculer_kpi(jour)

        self.assertGreater(kpi["km_du_jour"], 80)
        # Et rien ne déborde sur le lendemain.
        self.assertEqual(calculer_kpi(jour + timedelta(days=1))["km_du_jour"], 0)

    def test_le_tableau_de_bord_et_le_rapport_comptent_les_memes_km(self):
        """Les deux écrans doivent donner le même chiffre, sinon on ne sait
        plus lequel croire."""
        jour = timezone.localdate()
        self._position_locale(jour, 23, 0, COTONOU)
        self._position_locale(jour, 23, 30, BOHICON)

        kpi = calculer_kpi(jour)
        rapport = services.donnees_rapport_quotidien(jour)

        self.assertEqual(kpi["km_du_jour"], rapport["total_km"])
        self.assertEqual(kpi["km_par_camion"], rapport["kilometres"])


class BudgetDeRequetesTest(TestCase):
    """Le coût de calculer_kpi ne doit pas croître avec la flotte.

    Le cahier des charges demande une page en moins d'une seconde, rafraîchie
    toutes les dix secondes : le nombre de requêtes est donc la contrainte
    principale, et il doit être constant.
    """

    def _flotte(self, nombre):
        # get_or_create et un nom de chauffeur indexe : la methode est
        # appelee deux fois dans le meme test, pour agrandir la flotte.
        fournisseur, _ = FournisseurGPS.objects.get_or_create(nom="Cartrack")
        rang_depart = Vehicule.objects.count()
        chauffeur = creer_chauffeur(
            nom=f"Chauffeur {rang_depart}", permis=f"BJ-KP-{rang_depart}"
        )
        aujourdhui = timezone.localdate()
        maintenant = timezone.now()
        for index in range(nombre):
            vehicule = creer_vehicule(
                immatriculation=f"KP {1000 + rang_depart + index} RB",
                fournisseur_gps=fournisseur,
                boitier_id=f"CT-{index}",
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

    def test_le_nombre_de_requetes_est_le_meme_a_5_et_a_26_camions(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._flotte(5)
        with CaptureQueriesContext(connection) as avec_cinq:
            calculer_kpi()

        self._flotte(21)
        self.assertEqual(Vehicule.objects.count(), 26)
        with CaptureQueriesContext(connection) as avec_vingt_six:
            calculer_kpi()

        self.assertEqual(
            len(avec_cinq),
            len(avec_vingt_six),
            "le nombre de requêtes croît avec la flotte : un N+1 s'est glissé "
            f"({len(avec_cinq)} puis {len(avec_vingt_six)})",
        )

    def test_le_compte_exact(self):
        """Fixé volontairement, pour qu'une régression se voie.

        Six requêtes, et aucune de plus :

        1. la flotte active, avec le dernier relevé GPS de chaque camion en
           sous-requêtes corrélées ;
        2. les documents de cette flotte (prefetch_related) ;
        3. les positions du jour, pour les kilomètres ;
        4. les pleins, pour la consommation pondérée ;
        5. les chauffeurs, pour les échéances de permis ;
        6. les alertes ouvertes.

        La première version en comptait sept : collecter_echeances rechargeait
        la même flotte pour son propre compte. Lui passer la liste déjà en
        main a supprimé cette requête — sur une page rafraîchie toutes les dix
        secondes, cela se voit.
        """
        self._flotte(5)
        with self.assertNumQueries(6):
            calculer_kpi()
