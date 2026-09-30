"""Fabriques d'objets pour les tests.

Un test doit donner à lire ce qu'il vérifie, pas la liste des champs
obligatoires d'un modèle. Ces fonctions remplissent les valeurs par défaut ;
le test ne précise que ce qui compte pour lui — un permis expiré, un compteur
à 100 000 km.

Le fichier ne s'appelle pas test_quelque_chose : Django ne le prend donc pas
pour une suite de tests, il n'est qu'un module importé par les autres.
"""

from datetime import timedelta

from django.utils import timezone

from fleet.models import Chauffeur, Document, Vehicule


def creer_vehicule(immatriculation="AA 0001 RB", **champs):
    """Un véhicule actif et disponible, sauf indication contraire."""
    valeurs = {
        "marque": "Toyota",
        "modele": "Hiace",
        "annee": 2019,
        "type_vehicule": Vehicule.TypeVehicule.UTILITAIRE,
        "kilometrage": 100_000,
    }
    valeurs.update(champs)
    return Vehicule.objects.create(immatriculation=immatriculation, **valeurs)


def creer_chauffeur(nom="Rodrigue Hounkpatin", permis="BJ-0001", **champs):
    """Un chauffeur actif dont le permis est valide un an, sauf indication."""
    valeurs = {
        "telephone": "+229 01 97 00 00 00",
        "date_expiration_permis": timezone.localdate() + timedelta(days=365),
    }
    valeurs.update(champs)
    return Chauffeur.objects.create(nom=nom, numero_permis=permis, **valeurs)


def creer_document(vehicule, type_document=None, jours=90, **champs):
    """Un document expirant dans `jours` jours (négatif pour une pièce expirée)."""
    valeurs = {
        "type_document": type_document or Document.TypeDocument.ASSURANCE,
        "date_expiration": timezone.localdate() + timedelta(days=jours),
    }
    valeurs.update(champs)
    return Document.objects.create(vehicule=vehicule, **valeurs)
