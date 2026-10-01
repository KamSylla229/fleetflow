"""Configuration de l'interface d'administration de FleetFlow.

Pour chaque modèle on définit trois réglages :
  - list_display   : les colonnes affichées dans la liste ;
  - list_filter    : les filtres proposés dans le panneau de droite ;
  - search_fields  : les champs parcourus par la barre de recherche.

L'admin reste l'outil de saisie de secours et de dépannage. L'interface
utilisateur de FleetFlow, elle, vit dans fleet/views.py et ses gabarits.
"""

from django.contrib import admin

from .models import (
    Chauffeur,
    Document,
    Entretien,
    FournisseurGPS,
    Mission,
    PleinCarburant,
    PositionGPS,
    Vehicule,
)


class DocumentInline(admin.TabularInline):
    """Les documents d'un véhicule, saisis depuis la fiche du véhicule.

    Un inline évite d'avoir à créer le véhicule, puis à le rechercher depuis
    l'écran des documents pour lui rattacher son assurance.
    """

    model = Document
    extra = 1
    fields = ("type_document", "numero", "date_emission", "date_expiration")


@admin.register(Vehicule)
class VehiculeAdmin(admin.ModelAdmin):
    list_display = (
        "immatriculation",
        "marque",
        "modele",
        "type_vehicule",
        "statut",
        "kilometrage",
        "echeance_assurance",
        "echeance_visite_technique",
        "actif",
    )
    list_filter = ("statut", "type_vehicule", "actif")
    search_fields = ("immatriculation", "marque", "modele")
    inlines = (DocumentInline,)

    def get_queryset(self, request):
        """Précharge les documents de tous les véhicules de la page.

        Les colonnes d'échéance passent par les propriétés du modèle, qui
        lisent self.documents.all(). Sans ce prefetch_related, afficher une
        page de cent véhicules déclencherait cent requêtes supplémentaires —
        le problème « N+1 ». Avec, il n'en faut qu'une seule pour tous.
        """
        return super().get_queryset(request).prefetch_related("documents")

    # @admin.display transforme une méthode en colonne affichable.
    # "description" est le titre de la colonne dans l'admin. On passe par une
    # méthode plutôt que par la propriété brute pour formater le nombre de
    # jours et distinguer le cas « aucun document enregistré ».
    @admin.display(description="Assurance")
    def echeance_assurance(self, obj):
        jours = obj.jours_avant_fin_assurance
        # is None et non "if not jours" : zéro jour restant est une valeur
        # légitime (l'assurance expire aujourd'hui) et vaut False en Python.
        if jours is None:
            return "aucune attestation"
        if jours < 0:
            return f"expirée depuis {abs(jours)} j"
        return f"{jours} j"

    @admin.display(description="Visite technique")
    def echeance_visite_technique(self, obj):
        document = obj.document_visite_technique
        if document is None:
            return "aucun procès-verbal"
        jours = document.jours_avant_expiration
        if jours < 0:
            return f"dépassée depuis {abs(jours)} j"
        return f"{jours} j"


@admin.register(Chauffeur)
class ChauffeurAdmin(admin.ModelAdmin):
    list_display = (
        "nom",
        "telephone",
        "numero_permis",
        "date_expiration_permis",
        "echeance_permis",
        "actif",
    )
    list_filter = ("actif",)
    search_fields = ("nom", "telephone", "numero_permis")

    @admin.display(description="Permis")
    def echeance_permis(self, obj):
        jours = obj.jours_avant_expiration_permis
        if jours < 0:
            return f"expiré depuis {abs(jours)} j"
        return f"{jours} j"


@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = (
        "date_depart",
        "vehicule",
        "chauffeur",
        "depart",
        "destination",
        "statut",
        "distance",
    )
    list_filter = ("statut", "date_depart")
    # Le double underscore permet de chercher dans un modèle lié : ici la
    # plaque du véhicule rattaché à la mission.
    search_fields = ("destination", "depart", "vehicule__immatriculation")
    # Sans cette ligne, afficher le véhicule et le chauffeur de chaque mission
    # déclencherait deux requêtes SQL supplémentaires par ligne de la liste.
    list_select_related = ("vehicule", "chauffeur")


@admin.register(Entretien)
class EntretienAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "vehicule",
        "type_entretien",
        "km",
        "cout",
        "prestataire",
        "prochaine_echeance_km",
    )
    list_filter = ("type_entretien", "date")
    search_fields = ("vehicule__immatriculation", "prestataire")
    list_select_related = ("vehicule",)


@admin.register(PleinCarburant)
class PleinCarburantAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "vehicule",
        "chauffeur",
        "litres",
        "prix_litre",
        "km_compteur",
    )
    list_filter = ("date", "vehicule__type_vehicule")
    search_fields = ("vehicule__immatriculation", "chauffeur__nom")
    list_select_related = ("vehicule", "chauffeur")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "vehicule",
        "type_document",
        "numero",
        "date_emission",
        "date_expiration",
        "jours_restants",
    )
    list_filter = ("type_document", "vehicule__type_vehicule")
    search_fields = ("vehicule__immatriculation", "numero")
    list_select_related = ("vehicule",)

    @admin.display(description="Échéance")
    def jours_restants(self, obj):
        jours = obj.jours_avant_expiration
        if jours is None:
            return "sans échéance"
        if jours < 0:
            return f"expiré depuis {abs(jours)} j"
        return f"{jours} j"


@admin.register(FournisseurGPS)
class FournisseurGPSAdmin(admin.ModelAdmin):
    list_display = (
        "nom",
        "statut_connexion",
        "frequence_secondes",
        "dernier_echange",
        "camions_appaires",
    )
    list_filter = ("statut_connexion",)
    search_fields = ("nom",)

    @admin.display(description="Camions appairés")
    def camions_appaires(self, obj):
        return obj.vehicules.count()


@admin.register(PositionGPS)
class PositionGPSAdmin(admin.ModelAdmin):
    list_display = (
        "horodatage",
        "vehicule",
        "latitude",
        "longitude",
        "vitesse_kmh",
        "source",
    )
    list_filter = ("source", "vehicule__fournisseur_gps")
    search_fields = ("vehicule__immatriculation",)
    list_select_related = ("vehicule",)
    # La table grossit de plusieurs milliers de lignes par jour : une
    # navigation par date évite de dérouler des pages de pagination pour
    # retrouver un relevé.
    date_hierarchy = "horodatage"
