"""Fait avancer la flotte sur ses itineraires et enregistre ses positions.

Tant qu'aucun vrai boitier n'est raccorde, c'est cette commande qui joue le
role du fournisseur GPS. Elle n'ecrit rien elle-meme : tout passe par
fleet/services.py, afin qu'un futur travail planifie ou un bouton dans
l'interface produisent exactement les memes positions.

    python manage.py simuler_positions                      un seul tick
    python manage.py simuler_positions --boucle             en continu
    python manage.py simuler_positions --boucle --intervalle 10
    python manage.py simuler_positions --couper "AB 1234 RB"
    python manage.py simuler_positions --retablir "AB 1234 RB"

Toutes les sorties de cette commande sont en ASCII. La console Windows ecrit
en cp1252 : une fleche ou un emoji y provoque une UnicodeEncodeError, et comme
le seed l'a appris le 30/09, une erreur d'affichage dans une transaction
annule tout le travail accompli.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from fleet import services
from fleet.models import Vehicule


class Command(BaseCommand):
    help = (
        "Avance la flotte sur ses itineraires et enregistre les positions GPS. "
        "Reserve a la demonstration : les positions sont simulees."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--boucle",
            action="store_true",
            help="Tourne en continu jusqu'a Ctrl+C au lieu d'un seul tick.",
        )
        parser.add_argument(
            "--intervalle",
            type=int,
            default=5,
            help="Secondes entre deux ticks en mode boucle (defaut : 5).",
        )
        parser.add_argument(
            "--couper",
            metavar="IMMAT",
            help="Simule un boitier muet sur ce camion, puis s'arrete.",
        )
        parser.add_argument(
            "--retablir",
            metavar="IMMAT",
            help="Retablit le signal de ce camion, puis s'arrete.",
        )

    def handle(self, *args, **options):
        # Les deux interrupteurs sont des actions ponctuelles : on les traite
        # et on sort, sans lancer de simulation.
        if options["couper"]:
            self._basculer(options["couper"], coupe=True)
            return
        if options["retablir"]:
            self._basculer(options["retablir"], coupe=False)
            return

        intervalle = options["intervalle"]
        if intervalle <= 0:
            raise CommandError("--intervalle doit etre strictement positif.")

        if not options["boucle"]:
            self._tick(intervalle)
            return

        self.stdout.write(
            f"Simulation en continu, un tick toutes les {intervalle} s. "
            "Ctrl+C pour arreter."
        )
        try:
            while True:
                self._tick(intervalle)
                time.sleep(intervalle)
        except KeyboardInterrupt:
            # Sans ce bloc, Ctrl+C affiche une trace d'appels de vingt lignes
            # qui donne l'impression que quelque chose a casse.
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Simulation arretee."))

    # --- Actions -----------------------------------------------------------

    def _basculer(self, immatriculation, *, coupe):
        """Coupe ou retablit le signal d'un camion designe par sa plaque."""
        try:
            vehicule = Vehicule.objects.get(immatriculation=immatriculation)
        except Vehicule.DoesNotExist:
            raise CommandError(
                f"Aucun camion immatricule {immatriculation!r}. "
                "Verifiez la plaque, espaces compris."
            )

        vehicule = services.basculer_signal(vehicule, coupe=coupe)
        if vehicule.signal_coupe:
            self.stdout.write(
                self.style.WARNING(
                    f"{vehicule.immatriculation} : signal coupe. Son statut GPS "
                    "passera a \"sans signal\" une fois le seuil depasse."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{vehicule.immatriculation} : signal retabli. Le prochain "
                    "tick produira une position."
                )
            )

    def _tick(self, dt_secondes):
        """Un pas de simulation, puis le compte rendu."""
        maintenant = timezone.now()
        resultats = services.avancer_flotte(dt_secondes, maintenant=maintenant)

        deplaces = [(v, p) for v, p in resultats if p is not None]

        self.stdout.write(f"[{maintenant:%H:%M:%S}] {len(deplaces)} position(s) :")
        for vehicule, position in deplaces:
            etat = "arret" if position.vitesse_kmh == 0 else f"{position.vitesse_kmh} km/h"
            self.stdout.write(
                f"  {vehicule.immatriculation:12} {etat:>12}  "
                f"{position.latitude}, {position.longitude}  "
                f"progression {vehicule.progression * 100:5.1f} %"
            )

        if not deplaces:
            self.stdout.write(
                "  aucun camion a deplacer : il faut une mission en cours, un "
                "itineraire et un signal actif."
            )

        # Phase C branchera ici l'appel a services.verifier_signaux(), qui
        # ouvrira une alerte et previendra le gerant quand un boitier se tait.
        # Le point d'insertion est volontairement nomme pour qu'il ne se perde
        # pas dans la boucle.
