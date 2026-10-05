"""Le rapport d'activité d'une journée : kilomètres, carburant, alertes, échéances.

    python manage.py rapport_quotidien                      affiche le rapport
    python manage.py rapport_quotidien --date 2026-10-01     une journée passée
    python manage.py rapport_quotidien --email               l'envoie au gérant
    python manage.py rapport_quotidien --email --a chef@…    à quelqu'un d'autre

**Par défaut, rien n'est envoyé.** Le rapport s'affiche, un point. L'envoi
demande `--email`, explicitement : une commande qui écrit dans une boîte de
réception sans qu'on l'ait demandé est une commande qu'on n'ose plus lancer
pour voir.

C'est cette commande que la tâche planifiée appellera, avec `--email`.

La commande ne calcule rien : tout vient de
services.donnees_rapport_quotidien(), que les deux gabarits d'e-mail lisent
aussi. Si la version texte et la version HTML faisaient leurs propres totaux,
elles finiraient par ne plus dire la même chose.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date

from fleet import services

# Les couleurs des trois blocs d'échéances dans l'e-mail HTML. Elles sont
# écrites en dur parce qu'un client de messagerie ne connaît ni feuille de
# style externe, ni variable CSS — c'est le seul endroit du projet où
# dupliquer les jetons est le bon choix.
BLOCS_ECHEANCES = (
    ("depassees", "Dépassées", "#B3322C"),
    ("sous_30_j", "Sous 30 jours", "#8F5A12"),
    ("sous_60_j", "Sous 60 jours", "#5C6660"),
)


class Command(BaseCommand):
    help = (
        "Affiche le rapport d'activité d'une journée. Avec --email, l'envoie "
        "au gérant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            metavar="AAAA-MM-JJ",
            help="Jour du rapport. Par défaut, aujourd'hui.",
        )
        parser.add_argument(
            "--email",
            action="store_true",
            help="Envoie le rapport par e-mail au lieu de se contenter de l'afficher.",
        )
        parser.add_argument(
            "--a",
            metavar="ADRESSE",
            help="Destinataire, à la place de FLEETFLOW_EMAIL_GERANT.",
        )

    def handle(self, *args, **options):
        jour = self._lire_jour(options["date"])

        # Les échéances sont revérifiées avant d'être listées : sans cela, le
        # rapport pourrait annoncer une pièce dépassée sans qu'aucune alerte
        # ne soit ouverte en base, et le gérant verrait deux comptes
        # différents selon l'écran.
        services.verifier_echeances()

        donnees = services.donnees_rapport_quotidien(jour)
        contexte = dict(donnees)
        contexte["blocs_echeances"] = [
            {"titre": titre, "items": donnees["classement"][cle], "couleur": couleur}
            for cle, titre, couleur in BLOCS_ECHEANCES
        ]

        texte = render_to_string("emails/rapport_quotidien.txt", contexte)
        self.stdout.write(texte)

        if not options["email"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "Rapport affiché. Ajoutez --email pour l'envoyer au gérant."
                )
            )
            return

        self._envoyer(contexte, options["a"])

    # --- Outils ------------------------------------------------------------

    def _lire_jour(self, texte):
        """Convertit --date, ou renvoie la date du jour dans le fuseau local."""
        if not texte:
            return timezone.localdate()
        try:
            jour = parse_date(texte)
        except ValueError:
            jour = None
        if jour is None:
            raise CommandError(
                f"Date illisible : {texte!r}. Format attendu : AAAA-MM-JJ, "
                "par exemple 2026-10-01."
            )
        return jour

    def _envoyer(self, contexte, destinataire_force):
        """Envoie le rapport en texte avec une variante HTML."""
        destinataire = destinataire_force or settings.FLEETFLOW_EMAIL_GERANT
        if not destinataire:
            raise CommandError(
                "Aucun destinataire : renseignez FLEETFLOW_EMAIL_GERANT dans "
                "le .env, ou passez --a adresse@exemple.test."
            )

        jour = contexte["jour"]
        message = EmailMultiAlternatives(
            subject=f"[FleetFlow] Rapport du {jour:%d/%m/%Y}",
            body=render_to_string("emails/rapport_quotidien.txt", contexte),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[destinataire],
        )
        # Le texte reste le corps principal et le HTML une variante : un client
        # qui n'affiche pas le HTML montre un rapport lisible, pas du balisage.
        message.attach_alternative(
            render_to_string("emails/rapport_quotidien.html", contexte), "text/html"
        )

        try:
            message.send(fail_silently=False)
        except Exception as erreur:
            # Ici, contrairement aux alertes, l'échec doit remonter : une tâche
            # planifiée qui n'envoie rien doit échouer bruyamment, sinon
            # personne ne s'aperçoit que le rapport ne part plus.
            raise CommandError(
                f"L'envoi a échoué : {erreur}. Vérifiez les réglages EMAIL_* "
                "du .env."
            )

        self.stdout.write(self.style.SUCCESS(f"Rapport envoyé à {destinataire}."))
