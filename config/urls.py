"""Routes de premier niveau du projet FleetFlow.

Ce fichier ne contient que l'aiguillage general. Les routes metier vivent dans
fleet/urls.py, incluses ici sous leur propre espace de noms.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # django.contrib.auth.urls fournit des vues deja ecrites et testees par
    # Django : login, logout, changement et reinitialisation de mot de passe.
    # Elles cherchent leurs gabarits dans templates/registration/ ; nous ne
    # fournissons que login.html, les autres routes restant inutilisees pour
    # l'instant. Reecrire une vue de connexion a la main serait du code en
    # plus a maintenir, et une occasion de se tromper sur la securite.
    path("", include("django.contrib.auth.urls")),
]
