"""Configuration de l'interface d'administration de FleetFlow.

Pour chaque modèle on définit trois réglages :
  - list_display   : les colonnes affichées dans la liste ;
  - list_filter    : les filtres proposés dans le panneau de droite ;
  - search_fields  : les champs parcourus par la barre de recherche.
"""

from django.contrib import admin

from .models import Chauffeur, Entretien, Mission, PleinCarburant, Vehicule


@admin.register(Vehicule)
class VehiculeAdmin(admin.ModelAdmin):
    list_display = (
        "immatriculation",
        "marque",
        "modele",
        "type_vehicule",
        "statut",
        "kilometrage",
        "date_fin_assurance",
        "jours_restants_assurance",
        "date_visite_technique",
        "actif",
    )
    list_filter = ("statut", "type_vehicule", "actif")
    search_fields = ("immatriculation", "marque", "modele")

    # @admin.display transforme une méthode en colonne affichable.
    # "description" est le titre de la colonne dans l'admin. On passe par une
    # méthode plutôt que par la propriété brute pour ajouter le mot « jours »
    # et signaler visuellement les assurances déjà expirées.
    @admin.display(description="Assurance")
    def jours_restants_assurance(self, obj):
        jours = obj.jours_avant_fin_assurance
        if jours < 0:
            return f"expirée depuis {abs(jours)} j"
        return f"{jours} j"


@admin.register(Chauffeur)
class ChauffeurAdmin(admin.ModelAdmin):
    list_display = (
        "nom",
        "telephone",
        "numero_permis",
        "date_expiration_permis",
        "actif",
    )
    list_filter = ("actif",)
    search_fields = ("nom", "telephone", "numero_permis")


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
