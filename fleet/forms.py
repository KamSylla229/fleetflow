"""Formulaires de FleetFlow.

Un formulaire valide la *forme* de la saisie : champ obligatoire, nombre entier,
date bien écrite. Il ne valide pas les règles de gestion — « ce véhicule est
déjà engagé » n'est pas une question de saisie, et la réponse doit être la même
que la demande vienne d'un navigateur, d'un script ou de la commande de seed.
Ces règles vivent donc dans services.py, et les vues rattachent leurs messages
au formulaire.
"""

from django import forms

from .models import Chauffeur, Entretien, Mission, PleinCarburant, Vehicule


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


class MissionForm(MiseEnFormeBootstrapMixin, forms.ModelForm):
    """Affectation d'un véhicule et d'un chauffeur à une nouvelle mission.

    Ce formulaire n'enregistre jamais rien lui-même : la vue ne l'utilise que
    pour valider la *saisie*, puis passe les valeurs à
    services.creer_mission(), qui seul décide si l'affectation est possible.
    C'est pour cela que `statut` et `date_arrivee` sont absents — ce ne sont
    pas des informations à saisir, ce sont des conséquences.

    Les listes déroulantes ne proposent que les véhicules et chauffeurs de la
    flotte active. Elles ne masquent volontairement pas ceux qui sont déjà en
    mission : l'utilisateur doit pouvoir tenter l'affectation et lire le refus,
    qui lui apprend quelle mission bloque et comment la clôturer. Un choix
    silencieusement absent d'une liste ne s'explique pas de lui-même.
    """

    class Meta:
        model = Mission
        fields = (
            "vehicule",
            "chauffeur",
            "depart",
            "destination",
            "date_depart",
            "km_depart",
            "commentaire",
        )
        help_texts = {
            "date_depart": "Aujourd'hui ou une date passée : la mission démarre immédiatement.",
            "km_depart": "Laisser vide pour reprendre le compteur du véhicule.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicule"].queryset = Vehicule.objects.filter(actif=True)
        self.fields["chauffeur"].queryset = Chauffeur.objects.filter(actif=True)
        # Le service reprend le compteur du véhicule quand rien n'est saisi.
        self.fields["km_depart"].required = False
        self.fields["commentaire"].widget.attrs["rows"] = 2


class ClotureMissionForm(MiseEnFormeBootstrapMixin, forms.Form):
    """Relevés de retour d'une mission.

    Un forms.Form et non un ModelForm : on ne crée pas un objet, on complète
    trois champs d'une mission existante. Un ModelForm demanderait de lui
    passer l'instance et laisserait croire qu'un form.save() suffirait — alors
    que la clôture met aussi à jour le compteur et le statut du véhicule, ce
    que seul le service sait faire.
    """

    date_arrivee = forms.DateField(
        label="Date d'arrivée",
        widget=forms.DateInput,
    )
    km_arrivee = forms.IntegerField(
        label="Kilométrage à l'arrivée",
        min_value=0,
    )
    commentaire = forms.CharField(
        label="Commentaire",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class PleinCarburantForm(MiseEnFormeBootstrapMixin, forms.ModelForm):
    """Saisie d'un passage à la pompe."""

    class Meta:
        model = PleinCarburant
        fields = (
            "vehicule",
            "chauffeur",
            "date",
            "litres",
            "prix_litre",
            "km_compteur",
        )
        help_texts = {
            "km_compteur": (
                "Relevé du compteur à la pompe. Il doit dépasser celui du "
                "plein précédent, sinon la consommation serait fausse."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicule"].queryset = Vehicule.objects.filter(actif=True)
        self.fields["chauffeur"].queryset = Chauffeur.objects.filter(actif=True)


class EntretienForm(MiseEnFormeBootstrapMixin, forms.ModelForm):
    """Saisie d'une intervention de maintenance."""

    class Meta:
        model = Entretien
        fields = (
            "vehicule",
            "type_entretien",
            "date",
            "km",
            "cout",
            "prestataire",
            "prochaine_echeance_km",
        )
        help_texts = {
            "prochaine_echeance_km": (
                "Laisser vide pour appliquer l'intervalle habituel du type "
                "d'intervention (5 000 km pour une vidange, par exemple)."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vehicule"].queryset = Vehicule.objects.filter(actif=True)
        self.fields["prochaine_echeance_km"].required = False
