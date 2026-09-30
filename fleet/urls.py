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
]
