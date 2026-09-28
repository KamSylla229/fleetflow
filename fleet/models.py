"""Modèles de données de FleetFlow : la flotte, les conducteurs et leur activité.

Les modèles sont rangés du plus indépendant au plus dépendant : Vehicule et
Chauffeur d'abord, puis Mission, Entretien et PleinCarburant qui pointent
vers eux.
"""

from django.db import models
from django.utils import timezone


class Vehicule(models.Model):
    """Un véhicule de la flotte (camion, utilitaire, voiture ou moto)."""

    # Les "TextChoices" remplacent une liste de tuples : on écrit
    # Vehicule.TypeVehicule.CAMION dans le code (lisible, et une faute de frappe
    # se voit immédiatement), la base stocke la chaîne "camion", et l'admin
    # affiche le libellé "Camion".
    class TypeVehicule(models.TextChoices):
        CAMION = "camion", "Camion"
        UTILITAIRE = "utilitaire", "Utilitaire"
        VOITURE = "voiture", "Voiture"
        MOTO = "moto", "Moto"

    class Statut(models.TextChoices):
        DISPONIBLE = "disponible", "Disponible"
        EN_MISSION = "en_mission", "En mission"
        EN_MAINTENANCE = "en_maintenance", "En maintenance"
        HORS_SERVICE = "hors_service", "Hors service"

    # unique=True crée un index unique en base : impossible d'enregistrer
    # deux fois la même plaque, même par erreur de saisie.
    immatriculation = models.CharField(
        max_length=20,
        unique=True,
        verbose_name="Immatriculation",
        help_text="Format béninois, par exemple AB 1234 RB",
    )
    marque = models.CharField(max_length=50, verbose_name="Marque")
    modele = models.CharField(max_length=50, verbose_name="Modèle")
    annee = models.PositiveIntegerField(verbose_name="Année de mise en circulation")
    type_vehicule = models.CharField(
        max_length=20,
        choices=TypeVehicule.choices,
        verbose_name="Type de véhicule",
    )
    # PositiveIntegerField : un kilométrage ne peut pas être négatif,
    # la contrainte est posée au niveau de la base de données.
    kilometrage = models.PositiveIntegerField(default=0, verbose_name="Kilométrage")
    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.DISPONIBLE,
        verbose_name="Statut",
    )
    date_fin_assurance = models.DateField(verbose_name="Fin de l'assurance")
    date_visite_technique = models.DateField(verbose_name="Prochaine visite technique")
    # "actif" permet de sortir un véhicule de la flotte sans le supprimer :
    # on conserve ainsi tout son historique de missions et d'entretiens.
    actif = models.BooleanField(default=True, verbose_name="Actif")

    class Meta:
        ordering = ["immatriculation"]
        verbose_name = "Véhicule"
        verbose_name_plural = "Véhicules"

    def __str__(self):
        return f"{self.immatriculation} — {self.marque} {self.modele}"

    # --- Propriétés calculées ------------------------------------------------
    # Une @property n'est pas stockée en base : elle est recalculée à chaque
    # lecture. C'est exactement ce qu'on veut ici, car la réponse dépend de la
    # date du jour et changerait donc toute seule d'un jour à l'autre.

    @property
    def jours_avant_fin_assurance(self):
        """Nombre de jours restants avant la fin de l'assurance.

        La valeur est négative si l'assurance est déjà expirée
        (par exemple -3 pour une assurance expirée depuis trois jours).
        """
        # localdate() renvoie la date du jour dans le fuseau du projet
        # (Africa/Porto-Novo) et non en UTC : indispensable pour ne pas se
        # tromper d'un jour sur une échéance.
        ecart = self.date_fin_assurance - timezone.localdate()
        return ecart.days

    @property
    def assurance_expiree(self):
        """True si la date de fin d'assurance est déjà passée."""
        return self.jours_avant_fin_assurance < 0


class Chauffeur(models.Model):
    """Un conducteur habilité à prendre des missions."""

    nom = models.CharField(max_length=100, verbose_name="Nom complet")
    telephone = models.CharField(max_length=20, verbose_name="Téléphone")
    numero_permis = models.CharField(
        max_length=50,
        unique=True,
        verbose_name="Numéro de permis",
    )
    date_expiration_permis = models.DateField(verbose_name="Expiration du permis")
    actif = models.BooleanField(default=True, verbose_name="Actif")

    class Meta:
        ordering = ["nom"]
        verbose_name = "Chauffeur"
        verbose_name_plural = "Chauffeurs"

    def __str__(self):
        return self.nom


class Mission(models.Model):
    """Un déplacement confié à un chauffeur avec un véhicule."""

    class Statut(models.TextChoices):
        PLANIFIEE = "planifiee", "Planifiée"
        EN_COURS = "en_cours", "En cours"
        TERMINEE = "terminee", "Terminée"
        ANNULEE = "annulee", "Annulée"

    # on_delete=PROTECT : Django refuse la suppression d'un véhicule ou d'un
    # chauffeur tant qu'une mission le référence. L'historique d'exploitation
    # ne doit jamais disparaître à cause d'un clic malheureux.
    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.PROTECT,
        related_name="missions",
        verbose_name="Véhicule",
    )
    chauffeur = models.ForeignKey(
        Chauffeur,
        on_delete=models.PROTECT,
        related_name="missions",
        verbose_name="Chauffeur",
    )
    depart = models.CharField(max_length=100, verbose_name="Lieu de départ")
    destination = models.CharField(max_length=100, verbose_name="Destination")
    date_depart = models.DateField(verbose_name="Date de départ")
    # null=True : la colonne accepte NULL en base de données.
    # blank=True : le champ n'est pas obligatoire dans les formulaires.
    # Les deux sont nécessaires ici, car une mission planifiée ou en cours
    # n'a pas encore de date d'arrivée.
    date_arrivee = models.DateField(
        null=True,
        blank=True,
        verbose_name="Date d'arrivée",
    )
    km_depart = models.PositiveIntegerField(verbose_name="Kilométrage au départ")
    km_arrivee = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Kilométrage à l'arrivée",
    )
    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.PLANIFIEE,
        verbose_name="Statut",
    )
    # Un texte libre optionnel : blank=True suffit. Pour un champ texte, une
    # chaîne vide "" est préférable à NULL : cela évite d'avoir deux façons
    # différentes de représenter "pas de commentaire".
    commentaire = models.TextField(blank=True, verbose_name="Commentaire")

    class Meta:
        # Le signe moins inverse le tri : les missions les plus récentes
        # apparaissent en premier dans les listes et dans l'admin.
        ordering = ["-date_depart"]
        verbose_name = "Mission"
        verbose_name_plural = "Missions"

    def __str__(self):
        return f"{self.depart} → {self.destination} ({self.date_depart})"

    @property
    def distance(self):
        """Distance parcourue en kilomètres, ou None si la mission n'est pas terminée.

        Tant que le chauffeur n'a pas relevé le compteur à l'arrivée, km_arrivee
        vaut NULL : la distance est alors inconnue. On renvoie None plutôt que 0
        pour ne pas confondre « aucun kilomètre parcouru » et « information pas
        encore disponible ».
        """
        if self.km_arrivee is None:
            return None
        return self.km_arrivee - self.km_depart


class Entretien(models.Model):
    """Une intervention de maintenance réalisée sur un véhicule."""

    class TypeEntretien(models.TextChoices):
        VIDANGE = "vidange", "Vidange"
        PNEUS = "pneus", "Pneus"
        FREINS = "freins", "Freins"
        AUTRE = "autre", "Autre"

    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.PROTECT,
        related_name="entretiens",
        verbose_name="Véhicule",
    )
    type_entretien = models.CharField(
        max_length=20,
        choices=TypeEntretien.choices,
        verbose_name="Type d'entretien",
    )
    date = models.DateField(verbose_name="Date de l'intervention")
    km = models.PositiveIntegerField(verbose_name="Kilométrage à l'intervention")
    # DecimalField et non FloatField pour un montant : les nombres à virgule
    # flottante produisent des arrondis surprenants (0.1 + 0.2 ne fait pas
    # exactement 0.3), ce qui est inacceptable sur de l'argent. Le FCFA n'a pas
    # de centimes, mais on garde deux décimales au cas où un prestataire
    # facturerait en devise étrangère.
    cout = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="Coût (FCFA)",
    )
    prestataire = models.CharField(max_length=100, verbose_name="Prestataire")
    prochaine_echeance_km = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Prochaine échéance (km)",
        help_text="Kilométrage auquel le prochain entretien est attendu",
    )

    class Meta:
        ordering = ["-date"]
        verbose_name = "Entretien"
        verbose_name_plural = "Entretiens"

    def __str__(self):
        # get_<champ>_display() est généré automatiquement par Django dès qu'un
        # champ a des "choices" : il renvoie le libellé ("Vidange") et non la
        # valeur stockée ("vidange").
        return (
            f"{self.get_type_entretien_display()} — "
            f"{self.vehicule.immatriculation} ({self.date})"
        )


class PleinCarburant(models.Model):
    """Un ravitaillement en carburant, pour suivre la consommation réelle."""

    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.PROTECT,
        related_name="pleins",
        verbose_name="Véhicule",
    )
    chauffeur = models.ForeignKey(
        Chauffeur,
        on_delete=models.PROTECT,
        related_name="pleins",
        verbose_name="Chauffeur",
    )
    date = models.DateField(verbose_name="Date du plein")
    litres = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        verbose_name="Litres",
    )
    prix_litre = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        verbose_name="Prix du litre (FCFA)",
    )
    km_compteur = models.PositiveIntegerField(
        verbose_name="Kilométrage au compteur",
        help_text="Relevé du compteur au moment du plein",
    )

    class Meta:
        ordering = ["-date"]
        verbose_name = "Plein de carburant"
        verbose_name_plural = "Pleins de carburant"

    def __str__(self):
        return f"{self.vehicule.immatriculation} — {self.litres} L ({self.date})"
