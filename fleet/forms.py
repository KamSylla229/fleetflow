"""Formulaires de FleetFlow.

Un formulaire valide la *forme* de la saisie : champ obligatoire, nombre entier,
date bien écrite. Il ne valide pas les règles de gestion — « ce véhicule est
déjà engagé » n'est pas une question de saisie, et la réponse doit être la même
que la demande vienne d'un navigateur, d'un script ou de la commande de seed.
Ces règles vivent donc dans services.py, et les vues rattachent leurs messages
au formulaire.
"""

from django import forms

from .models import Chauffeur, Vehicule


class MiseEnFormeBootstrapMixin:
    """Applique les classes Bootstrap à tous les champs d'un formulaire.

    L'alternative serait de déclarer un `widgets = {...}` par formulaire, en
    répétant `attrs={"class": "form-control"}` sur chaque champ. C'est long,
    illisible, et une classe oubliée ne se voit qu'à l'écran. Ici la règle est
    écrite une fois et s'applique à tout formulaire qui hérite du mixin.

    Le mixin doit être placé avant forms.ModelForm dans la liste des bases :
    Python résout les attributs de gauche à droite, et c'est ce __init__ qui
    doit s'exécuter après celui de Django (super() y pourvoit).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for champ in self.fields.values():
            widget = champ.widget

            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            else:
                widget.attrs.setdefault("class", "form-control")

            if isinstance(widget, forms.DateInput):
                # input_type="date" affiche le sélecteur de date natif du
                # navigateur, sans aucune bibliothèque JavaScript. Il impose en
                # revanche le format ISO : sans la ligne suivante, la valeur
                # existante serait envoyée au gabarit au format français
                # (30/09/2026), que le champ natif refuse d'afficher — le
                # formulaire de modification apparaîtrait vide.
                widget.input_type = "date"
                widget.format = "%Y-%m-%d"


class VehiculeForm(MiseEnFormeBootstrapMixin, forms.ModelForm):
    """Création et modification d'un véhicule.

    `actif` est absent volontairement : la mise hors flotte n'est pas une
    modification comme une autre, elle passe par un bouton dédié et une requête
    POST. La laisser dans le formulaire permettrait de désactiver un véhicule
    par inadvertance en corrigeant sa marque.
    """

    class Meta:
        model = Vehicule
        fields = (
            "immatriculation",
            "marque",
            "modele",
            "annee",
            "type_vehicule",
            "kilometrage",
            "statut",
        )
        help_texts = {
            "kilometrage": (
                "Relevé actuel du compteur. Il sera ensuite mis à jour "
                "automatiquement par les clôtures de mission et les pleins."
            ),
        }


class ChauffeurForm(MiseEnFormeBootstrapMixin, forms.ModelForm):
    """Création et modification d'un chauffeur."""

    class Meta:
        model = Chauffeur
        fields = (
            "nom",
            "telephone",
            "numero_permis",
            "date_expiration_permis",
        )
