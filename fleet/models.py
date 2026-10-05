"""Modèles de données de FleetFlow : la flotte, les conducteurs et leur activité.

Les modèles sont rangés du plus indépendant au plus dépendant : Vehicule et
Chauffeur d'abord, puis Mission, Entretien et PleinCarburant qui pointent
vers eux.

Les échéances administratives (assurance, visite technique, carte grise…)
ne sont pas des champs de Vehicule : elles vivent dans le modèle Document,
en bas de ce fichier.
"""

from django.db import models
from django.urls import reverse
from django.utils import timezone

from .itineraires import CHOIX_ITINERAIRE, itineraire_par_code


class FournisseurGPS(models.Model):
    """Un prestataire de géolocalisation chez qui des boîtiers sont abonnés.

    Au Bénin, deux acteurs reviennent : Cartrack et Orange Fleet. Chacun
    expose ses positions à sa façon — interrogation périodique pour l'un,
    renvoi de flux pour l'autre. Le modèle ne décrit pas ces différences
    techniques : il garde l'état du raccordement, ce qu'un gestionnaire a
    besoin de voir quand les positions cessent d'arriver.
    """

    class StatutConnexion(models.TextChoices):
        CONNECTE = "connecte", "Connecté"
        EN_ATTENTE = "en_attente", "En attente"
        ERREUR = "erreur", "Erreur"

    nom = models.CharField(max_length=60, unique=True, verbose_name="Nom")
    statut_connexion = models.CharField(
        max_length=20,
        choices=StatutConnexion.choices,
        default=StatutConnexion.EN_ATTENTE,
        verbose_name="Statut du raccordement",
    )
    frequence_secondes = models.PositiveIntegerField(
        default=30,
        verbose_name="Fréquence d'interrogation (s)",
        help_text="Intervalle entre deux relevés de position chez ce fournisseur",
    )
    # Mis à jour par la simulation à chaque position créée. Null tant
    # qu'aucun échange n'a eu lieu : « jamais » et « il y a longtemps » ne
    # sont pas la même information.
    dernier_echange = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Dernier échange réussi",
    )

    class Meta:
        ordering = ["nom"]
        verbose_name = "Fournisseur GPS"
        verbose_name_plural = "Fournisseurs GPS"

    def __str__(self):
        return self.nom


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
    # Les échéances (assurance, visite technique, carte grise…) ne sont plus
    # des champs de ce modèle : elles sont portées par Document, en bas de ce
    # fichier. Voir les propriétés date_fin_assurance et date_visite_technique
    # plus bas, qui les relisent depuis le document en vigueur.

    # "actif" permet de sortir un véhicule de la flotte sans le supprimer :
    # on conserve ainsi tout son historique de missions et d'entretiens.
    actif = models.BooleanField(default=True, verbose_name="Actif")

    # --- Suivi GPS ---------------------------------------------------------
    # on_delete=SET_NULL et non PROTECT : perdre un fournisseur ne doit pas
    # bloquer la flotte. Un camion sans boîtier reste un camion ; il remonte
    # seulement « aucune donnée ».
    fournisseur_gps = models.ForeignKey(
        "FournisseurGPS",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vehicules",
        verbose_name="Fournisseur GPS",
    )
    boitier_id = models.CharField(
        max_length=40,
        blank=True,
        verbose_name="Identifiant du boîtier",
        help_text="Référence du boîtier chez le fournisseur, par exemple CT-88214",
    )

    # --- État de la simulation ---------------------------------------------
    # Ces quatre champs ne décrivent pas le camion mais le pantin qui le
    # remplace en attendant un vrai boîtier. Ils seront inutiles le jour où
    # les positions viendront d'un fournisseur ; les garder ici plutôt que
    # dans une table à part évite une jointure sur chaque tick pour des
    # données qui disparaîtront ensemble.
    itineraire = models.CharField(
        max_length=30,
        blank=True,
        choices=CHOIX_ITINERAIRE,
        verbose_name="Itinéraire simulé",
    )
    progression = models.FloatField(
        default=0,
        verbose_name="Progression sur l'itinéraire",
        help_text="0 au départ, 1 à l'arrivée",
    )
    direction = models.SmallIntegerField(
        default=1,
        choices=((1, "Aller"), (-1, "Retour")),
        verbose_name="Sens de parcours",
    )
    vitesse_nominale_kmh = models.PositiveIntegerField(
        default=60,
        verbose_name="Vitesse nominale (km/h)",
    )
    # Interrupteur de démonstration : il simule un boîtier muet sans rien
    # débrancher. C'est ce que bascule « simuler_positions --couper ».
    signal_coupe = models.BooleanField(
        default=False,
        verbose_name="Signal coupé",
        help_text="Simule un boîtier qui ne remonte plus rien",
    )

    class Meta:
        ordering = ["immatriculation"]
        # Le modèle garde le nom Vehicule (le renommer coûterait une migration
        # de table et des centaines de références, pour aucune valeur), mais
        # l'interface parle de « camion », le mot employé par les clients.
        # verbose_name est exactement fait pour cet écart entre le nom
        # technique et le nom métier : il alimente l'admin, les libellés de
        # formulaire et les messages d'erreur.
        verbose_name = "Camion"
        verbose_name_plural = "Camions"

    def __str__(self):
        return f"{self.immatriculation} — {self.marque} {self.modele}"

    def get_absolute_url(self):
        """L'adresse de la fiche de ce véhicule.

        Django s'en sert seul : CreateView et UpdateView y redirigent après
        enregistrement, et l'admin propose un lien « voir sur le site ».
        Définir l'URL ici évite de répéter un success_url dans chaque vue, et
        de devoir les corriger toutes le jour où l'adresse change.
        """
        return reverse("fleet:vehicule_detail", args=[self.pk])

    # --- Propriétés calculées ------------------------------------------------
    # Une @property n'est pas stockée en base : elle est recalculée à chaque
    # lecture. C'est exactement ce qu'on veut ici, car la réponse dépend de la
    # date du jour et changerait donc toute seule d'un jour à l'autre.

    def _dernier_document(self, type_document):
        """Le document en vigueur pour un type donné, ou None s'il n'y en a pas.

        Le tri est fait en Python sur self.documents.all(), et non avec un
        .filter() SQL. C'est volontaire : quand la vue a préchargé les documents
        avec prefetch_related("documents"), parcourir une liste déjà en mémoire
        ne coûte aucune requête. Un .filter() en déclencherait une nouvelle à
        chaque appel — donc vingt-cinq de plus sur une liste de vingt-cinq
        véhicules. C'est précisément le problème « N+1 » qu'on cherche à éviter.
        """
        candidats = [
            document
            for document in self.documents.all()
            if document.type_document == type_document
            and document.date_expiration is not None
        ]
        if not candidats:
            return None
        # Le document qui fait foi est celui dont l'échéance est la plus
        # lointaine : c'est le dernier renouvellement enregistré.
        return max(candidats, key=lambda document: document.date_expiration)

    @property
    def progression_pourcent(self):
        """Progression sur l'itinéraire, en pourcentage entier (0 à 100).

        Le champ `progression` vaut entre 0 et 1. Multiplier par 100 dans un
        gabarit demanderait {% widthratio %}, qu'on relit mal ; et la première
        tentative — « {{ progression|floatformat:0 }}0 % » — affichait
        tranquillement « 00 % » sans lever la moindre erreur.
        """
        return round(self.progression * 100)

    @property
    def trace(self):
        """L'objet Itineraire correspondant au code stocké, ou None.

        Le champ garde un code (« cotonou_parakou ») ; la géométrie vit dans
        fleet/itineraires.py. Cette propriété fait le lien, pour qu'aucun
        appelant n'ait à connaître le dictionnaire ITINERAIRES.
        """
        return itineraire_par_code(self.itineraire)

    @property
    def document_assurance(self):
        """L'attestation d'assurance en vigueur, ou None si aucune n'est saisie."""
        return self._dernier_document(Document.TypeDocument.ASSURANCE)

    @property
    def date_fin_assurance(self):
        """Fin de l'assurance en vigueur, ou None si aucune n'est enregistrée."""
        document = self.document_assurance
        return document.date_expiration if document else None

    @property
    def jours_avant_fin_assurance(self):
        """Nombre de jours restants avant la fin de l'assurance.

        La valeur est négative si l'assurance est déjà expirée (par exemple -3
        pour une assurance expirée depuis trois jours), et None si aucune
        attestation n'est enregistrée pour ce véhicule.
        """
        date_fin = self.date_fin_assurance
        if date_fin is None:
            return None
        # localdate() renvoie la date du jour dans le fuseau du projet
        # (Africa/Porto-Novo) et non en UTC : indispensable pour ne pas se
        # tromper d'un jour sur une échéance.
        return (date_fin - timezone.localdate()).days

    @property
    def assurance_expiree(self):
        """True si l'assurance est expirée, False sinon, None si on l'ignore.

        Trois états et non deux : « aucune attestation enregistrée » n'est pas
        « assurance valide ». Renvoyer False dans ce cas afficherait comme
        rassurant un véhicule dont le dossier est en réalité vide.
        """
        jours = self.jours_avant_fin_assurance
        if jours is None:
            return None
        return jours < 0

    @property
    def document_visite_technique(self):
        """Le dernier procès-verbal de visite technique, ou None."""
        return self._dernier_document(Document.TypeDocument.VISITE_TECHNIQUE)

    @property
    def date_visite_technique(self):
        """Échéance de la visite technique, ou None si elle n'est pas saisie."""
        document = self.document_visite_technique
        return document.date_expiration if document else None


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

    def get_absolute_url(self):
        return reverse("fleet:chauffeur_detail", args=[self.pk])

    # --- Propriétés calculées ------------------------------------------------

    @property
    def jours_avant_expiration_permis(self):
        """Jours restants avant l'expiration du permis (négatif si dépassée)."""
        return (self.date_expiration_permis - timezone.localdate()).days

    @property
    def permis_expire(self):
        """True si le permis n'est plus valide aujourd'hui.

        Deux états seulement ici, contrairement à assurance_expiree :
        date_expiration_permis est un champ obligatoire, la réponse est donc
        toujours connue. L'assurance, elle, dépend d'un document facultatif.
        """
        return self.jours_avant_expiration_permis < 0


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
        verbose_name="Camion",
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
        # Ces contraintes sont posées dans la base de données elle-même, pas
        # seulement en Python. Les fonctions de services.py valident déjà les
        # mêmes règles et renvoient des messages clairs à l'utilisateur ; la
        # contrainte est le filet de sécurité de dernier recours, qui protège
        # aussi les écritures faites depuis l'admin, un script ou le shell.
        constraints = [
            models.CheckConstraint(
                # Q(...) | Q(...) : soit la mission n'est pas clôturée (le
                # kilométrage d'arrivée vaut NULL), soit il dépasse strictement
                # celui du départ. Autoriser NULL explicitement est
                # indispensable : sans cela, toute mission en cours serait
                # refusée par la base.
                condition=models.Q(km_arrivee__isnull=True)
                | models.Q(km_arrivee__gt=models.F("km_depart")),
                name="mission_km_arrivee_superieur_km_depart",
                violation_error_message=(
                    "Le kilométrage d'arrivée doit être strictement supérieur "
                    "au kilométrage de départ."
                ),
            ),
            models.CheckConstraint(
                # F("date_depart") désigne la valeur de l'autre colonne de la
                # même ligne : la comparaison est faite par la base, sans
                # relire l'objet en Python.
                condition=models.Q(date_arrivee__isnull=True)
                | models.Q(date_arrivee__gte=models.F("date_depart")),
                name="mission_date_arrivee_apres_date_depart",
                violation_error_message=(
                    "La date d'arrivée ne peut pas précéder la date de départ."
                ),
            ),
        ]

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
        verbose_name="Camion",
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
        verbose_name="Camion",
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
        constraints = [
            models.CheckConstraint(
                # Un plein de zéro litre n'existe pas : c'est une saisie
                # erronée, et il provoquerait une division par zéro dans le
                # calcul de consommation.
                condition=models.Q(litres__gt=0),
                name="plein_litres_strictement_positifs",
                violation_error_message=(
                    "Le nombre de litres doit être strictement positif."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.vehicule.immatriculation} — {self.litres} L ({self.date})"

    @property
    def cout_total(self):
        """Montant dépensé pour ce plein, en FCFA.

        Le produit est fait en Decimal, comme les deux champs dont il vient :
        repasser par un flottant pour multiplier réintroduirait exactement les
        arrondis que DecimalField sert à éviter.

        Ce n'est pas stocké, pour la même raison que Mission.distance : la
        valeur se déduit de deux champs de la même ligne, et un troisième champ
        à maintenir cohérent serait une occasion de se contredire.
        """
        return self.litres * self.prix_litre


class Document(models.Model):
    """Une pièce administrative rattachée à un véhicule, avec son échéance.

    Ce modèle est la seule source de vérité pour les échéances d'un véhicule.
    Vehicule ne porte plus de champ date_fin_assurance ni date_visite_technique :
    dès qu'il a fallu gérer aussi une carte grise ou une licence de transport, la
    même information se serait retrouvée à deux endroits — et deux sources de
    vérité finissent toujours par diverger. Ici, un véhicule porte autant de
    documents que nécessaire, et l'historique des renouvellements est conservé.
    """

    class TypeDocument(models.TextChoices):
        ASSURANCE = "assurance", "Assurance"
        VISITE_TECHNIQUE = "visite_technique", "Visite technique"
        CARTE_GRISE = "carte_grise", "Carte grise"
        LICENCE_TRANSPORT = "licence_transport", "Licence de transport"
        AUTRE = "autre", "Autre"

    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name="Camion",
    )
    type_document = models.CharField(
        max_length=30,
        choices=TypeDocument.choices,
        verbose_name="Type de document",
    )
    numero = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Numéro de la pièce",
    )
    date_emission = models.DateField(
        null=True,
        blank=True,
        verbose_name="Date d'émission",
    )
    # Facultatif, car toutes les pièces n'expirent pas : une carte grise reste
    # valable tant que le véhicule ne change pas de propriétaire. Un document
    # sans échéance reçoit un badge neutre, jamais vert ni rouge.
    date_expiration = models.DateField(
        null=True,
        blank=True,
        verbose_name="Date d'expiration",
        help_text="À laisser vide pour une pièce sans échéance, comme la carte grise",
    )
    commentaire = models.TextField(blank=True, verbose_name="Commentaire")

    class Meta:
        # Échéances les plus lointaines d'abord : pour un type donné, le
        # document en vigueur est le premier de la liste.
        ordering = ["-date_expiration"]
        verbose_name = "Document"
        verbose_name_plural = "Documents"

    def __str__(self):
        if self.date_expiration is None:
            return (
                f"{self.get_type_document_display()} — "
                f"{self.vehicule.immatriculation}"
            )
        return (
            f"{self.get_type_document_display()} — "
            f"{self.vehicule.immatriculation} "
            f"(expire le {self.date_expiration:%d/%m/%Y})"
        )

    @property
    def jours_avant_expiration(self):
        """Jours restants avant l'expiration, ou None si la pièce n'expire pas.

        La qualification en valide / bientôt expiré / expiré n'est pas ici mais
        dans services.statut_echeance() : la même règle sert aussi au permis des
        chauffeurs, elle n'a donc pas à être dupliquée dans deux modèles.
        """
        if self.date_expiration is None:
            return None
        return (self.date_expiration - timezone.localdate()).days



class PositionGPS(models.Model):
    """Un relevé de position d'un camion, horodaté.

    Alimenté par la simulation en v1 (`manage.py simuler_positions`), par un
    webhook fournisseur en v2 — d'où le champ `source`, qui dira d'où vient
    chaque point le jour où les deux coexisteront.

    on_delete=CASCADE, contrairement à tout le reste du projet : une position
    n'a aucun sens sans son camion, et il n'y a pas d'historique comptable à
    protéger. Sortir un camion de la flotte ne supprime d'ailleurs rien,
    puisqu'on ne supprime jamais un camion.
    """

    class Source(models.TextChoices):
        SIMULATION = "simulation", "Simulation"
        WEBHOOK = "webhook", "Webhook fournisseur"

    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.CASCADE,
        related_name="positions",
        verbose_name="Camion",
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        verbose_name="Latitude",
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        verbose_name="Longitude",
    )
    vitesse_kmh = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        verbose_name="Vitesse (km/h)",
    )
    # default=timezone.now et non auto_now_add : il faut pouvoir semer un
    # historique daté d'hier, et simuler un boîtier dont le dernier point a
    # trois heures. auto_now_add écraserait toute valeur fournie.
    horodatage = models.DateTimeField(
        default=timezone.now,
        verbose_name="Horodatage",
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.SIMULATION,
        verbose_name="Source",
    )

    class Meta:
        ordering = ["-horodatage", "-pk"]
        verbose_name = "Position GPS"
        verbose_name_plural = "Positions GPS"
        indexes = [
            # La question posée en permanence est « la dernière position de ce
            # camion ». Sans cet index composé, chaque réponse demanderait de
            # parcourir toutes les positions du camion ; la table grossit de
            # plusieurs milliers de lignes par jour et par camion.
            models.Index(
                fields=["vehicule", "-horodatage"],
                name="position_vehicule_recent",
            ),
        ]

    def __str__(self):
        return (
            f"{self.vehicule.immatriculation} — "
            f"{self.latitude}, {self.longitude} "
            f"({self.horodatage:%d/%m/%Y %H:%M})"
        )


class Alerte(models.Model):
    """Un problème ouvert qui attend une action du gestionnaire.

    Deux familles pour l'instant : un boîtier qui ne remonte plus rien, et une
    échéance proche ou dépassée. Une alerte n'est pas un message : c'est un
    état. Elle s'ouvre quand le problème apparaît, se résout quand il
    disparaît, et garde la trace du moment où le gérant a été prévenu.

    Pourquoi un modèle plutôt qu'un simple calcul à l'affichage : parce qu'il
    faut se souvenir de deux choses qu'aucun calcul ne peut redonner — depuis
    quand le problème dure, et si l'e-mail est déjà parti. Sans cette mémoire,
    la simulation enverrait un courriel à chaque tick, soit toutes les cinq
    secondes.

    Les trois clés étrangères sont facultatives et une seule est renseignée à
    la fois : un boîtier muet concerne un camion, un permis périmé un
    chauffeur, une assurance expirée un document. Un modèle par type d'alerte
    aurait multiplié les tables pour la même logique ; une relation générique
    (ContentType) aurait rendu les requêtes et les jointures illisibles pour
    trois cas connus d'avance.
    """

    class TypeAlerte(models.TextChoices):
        SANS_SIGNAL = "sans_signal", "Boîtier sans signal"
        # Deux types d'échéance et non un seul : une pièce à renouveler et un
        # permis à refaire ne se traitent pas au même endroit ni par les mêmes
        # personnes. Les séparer rend aussi les contraintes d'unicité
        # lisibles — l'une porte sur le document, l'autre sur le chauffeur.
        ECHEANCE = "echeance", "Pièce à renouveler"
        ECHEANCE_PERMIS = "echeance_permis", "Permis à renouveler"

    # Le champ s'appelle type_alerte et non « type » : les autres modèles du
    # projet utilisent déjà type_vehicule, type_entretien et type_document, et
    # « type » masquerait la fonction intégrée du même nom dans le corps de la
    # classe.
    type_alerte = models.CharField(
        max_length=20,
        choices=TypeAlerte.choices,
        verbose_name="Type d'alerte",
    )

    # on_delete=CASCADE, comme pour les positions : une alerte n'a aucun sens
    # sans l'objet qu'elle concerne, et il n'y a pas d'historique comptable à
    # protéger.
    vehicule = models.ForeignKey(
        Vehicule,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alertes",
        verbose_name="Camion",
    )
    chauffeur = models.ForeignKey(
        Chauffeur,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alertes",
        verbose_name="Chauffeur",
    )
    document = models.ForeignKey(
        "Document",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alertes",
        verbose_name="Document",
    )

    message = models.TextField(verbose_name="Message")

    # default=timezone.now et non auto_now_add, pour la même raison que
    # PositionGPS.horodatage : il faut pouvoir semer une alerte ouverte depuis
    # trois heures.
    ouverte_le = models.DateTimeField(
        default=timezone.now,
        verbose_name="Ouverte le",
    )
    resolue_le = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Résolue le",
    )
    # Trace de l'envoi au gérant. Null signifie « pas encore envoyé » : c'est
    # ce qui permet de réessayer au passage suivant quand le serveur SMTP
    # était injoignable, sans jamais envoyer deux fois.
    email_envoye_le = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="E-mail envoyé le",
    )

    class Meta:
        ordering = ["resolue_le", "-ouverte_le"]
        verbose_name = "Alerte"
        verbose_name_plural = "Alertes"
        constraints = [
            # Une seule alerte ouverte par type et par objet concerné. Sans
            # cette contrainte, la simulation en créerait une par tick.
            #
            # Trois contraintes partielles et non une seule sur les trois
            # colonnes : en SQL, NULL n'est jamais égal à NULL, donc une
            # contrainte sur (type, vehicule, chauffeur, document) ne
            # dédoublonnerait rien dès qu'une colonne est vide — c'est-à-dire
            # toujours, puisque deux des trois le sont.
            models.UniqueConstraint(
                fields=["type_alerte", "vehicule"],
                condition=models.Q(resolue_le__isnull=True, vehicule__isnull=False),
                name="une_alerte_ouverte_par_camion",
            ),
            models.UniqueConstraint(
                fields=["type_alerte", "chauffeur"],
                condition=models.Q(resolue_le__isnull=True, chauffeur__isnull=False),
                name="une_alerte_ouverte_par_chauffeur",
            ),
            models.UniqueConstraint(
                fields=["type_alerte", "document"],
                condition=models.Q(resolue_le__isnull=True, document__isnull=False),
                name="une_alerte_ouverte_par_document",
            ),
        ]

    def __str__(self):
        return f"{self.get_type_alerte_display()} — {self.libelle_objet}"

    @property
    def objet(self):
        """L'objet concerné, quel qu'il soit, ou None.

        Évite aux appelants d'écrire trois `if` pour savoir laquelle des trois
        clés étrangères est renseignée.
        """
        return self.vehicule or self.chauffeur or self.document

    @property
    def libelle_objet(self):
        """De quoi parle l'alerte, en une ligne lisible."""
        if self.vehicule_id:
            return self.vehicule.immatriculation
        if self.chauffeur_id:
            return self.chauffeur.nom
        if self.document_id:
            return str(self.document)
        return "objet inconnu"

    @property
    def est_ouverte(self):
        return self.resolue_le is None

    @property
    def duree(self):
        """Durée du problème : jusqu'à sa résolution, ou jusqu'à maintenant.

        Un timedelta, que le gabarit affiche avec le filtre `timesince`.
        """
        fin = self.resolue_le or timezone.now()
        return fin - self.ouverte_le
