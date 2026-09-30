"""Routes métier de FleetFlow.

``app_name`` déclare un espace de noms : les routes s'écrivent
``fleet:vehicule_liste`` dans les gabarits et les redirections. Sans lui, un
nom de route donné deux fois dans le projet (par une future application
``facturation``, par exemple) écraserait silencieusement l'autre.

Chaque nom suit le même schéma, ``objet_action``, pour qu'on devine une route
sans avoir à ouvrir ce fichier.
"""

from django.urls import path

from . import views

app_name = "fleet"

urlpatterns = [
    path("", views.AccueilView.as_view(), name="accueil"),
    # --- Véhicules ---
    path("vehicules/", views.VehiculeListView.as_view(), name="vehicule_liste"),
    path(
        "vehicules/nouveau/",
        views.VehiculeCreateView.as_view(),
        name="vehicule_creer",
    ),
    # <int:pk> impose un entier : une adresse fantaisiste renvoie une 404 sans
    # jamais atteindre la vue ni la base de données.
    path(
        "vehicules/<int:pk>/",
        views.VehiculeDetailView.as_view(),
        name="vehicule_detail",
    ),
    path(
        "vehicules/<int:pk>/modifier/",
        views.VehiculeUpdateView.as_view(),
        name="vehicule_modifier",
    ),
    path(
        "vehicules/<int:pk>/activation/",
        views.vehicule_basculer_activation,
        name="vehicule_activation",
    ),
    # --- Chauffeurs ---
    path("chauffeurs/", views.ChauffeurListView.as_view(), name="chauffeur_liste"),
    path(
        "chauffeurs/nouveau/",
        views.ChauffeurCreateView.as_view(),
        name="chauffeur_creer",
    ),
    path(
        "chauffeurs/<int:pk>/",
        views.ChauffeurDetailView.as_view(),
        name="chauffeur_detail",
    ),
    path(
        "chauffeurs/<int:pk>/modifier/",
        views.ChauffeurUpdateView.as_view(),
        name="chauffeur_modifier",
    ),
    path(
        "chauffeurs/<int:pk>/activation/",
        views.chauffeur_basculer_activation,
        name="chauffeur_activation",
    ),
    # --- Missions ---
    path("missions/", views.MissionListView.as_view(), name="mission_liste"),
    path(
        "missions/nouvelle/",
        views.MissionCreateView.as_view(),
        name="mission_creer",
    ),
    path(
        "missions/<int:pk>/cloturer/",
        views.MissionClotureView.as_view(),
        name="mission_cloturer",
    ),
    # --- Carburant ---
    path("carburant/", views.PleinListView.as_view(), name="plein_liste"),
    path("carburant/nouveau/", views.PleinCreateView.as_view(), name="plein_creer"),
    # --- Entretiens ---
    path("entretiens/", views.EntretienListView.as_view(), name="entretien_liste"),
    path(
        "entretiens/nouveau/",
        views.EntretienCreateView.as_view(),
        name="entretien_creer",
    ),
    # --- Documents ---
    path("documents/", views.DocumentListView.as_view(), name="document_liste"),
]
