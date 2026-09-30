"""Vues de FleetFlow.

Le contrat que s'impose ce fichier : lire la requête, appeler services.py,
choisir un gabarit. Aucune règle de gestion ici — pas de calcul d'échéance, pas
de décision sur ce qui est autorisé. Quand une vue doit refuser quelque chose,
elle attrape la ValidationError du service et en fait un message Bootstrap.

Toutes les vues exigent une authentification : LoginRequiredMixin pour les
classes, @login_required pour les fonctions. C'est une vérification à ne jamais
oublier — un oubli n'échoue pas, il ouvre discrètement une page.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    RedirectView,
    UpdateView,
)

from . import services
from .forms import ChauffeurForm, VehiculeForm
from .models import Chauffeur, Vehicule


class AccueilView(LoginRequiredMixin, RedirectView):
    """La racine du site mène à la liste des véhicules.

    Le tableau de bord avec indicateurs n'existe pas encore ; en attendant,
    rediriger vaut mieux qu'une page vide, et l'adresse d'accueil ne changera
    pas le jour où ce tableau de bord arrivera.
    """

    pattern_name = "fleet:vehicule_liste"


class ListeFiltrableView(LoginRequiredMixin, ListView):
    """Socle commun aux listes : pagination et conservation des filtres.

    Les filtres passent par la chaîne de requête (?q=…&statut=…). Il faut donc
    les réinjecter dans les liens de pagination, sinon passer à la page 2
    effacerait la recherche — le genre de détail qui fait dire d'une interface
    qu'elle est « bizarre » sans qu'on sache pourquoi.
    """

    paginate_by = 25

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)

        # request.GET est immuable : on en prend une copie modifiable pour en
        # retirer le numéro de page.
        parametres = self.request.GET.copy()
        parametres.pop("page", None)
        contexte["parametres_filtres"] = parametres.urlencode()

        return contexte


# --- Véhicules ---------------------------------------------------------------


class VehiculeListView(ListeFiltrableView):
    model = Vehicule
    template_name = "fleet/vehicule_liste.html"
    context_object_name = "vehicules"

    def get_queryset(self):
        # prefetch_related et non select_related : documents est une relation
        # inverse (plusieurs documents par véhicule), que select_related ne
        # sait pas charger. Sans cela, les badges d'échéance déclencheraient
        # une requête par ligne affichée.
        queryset = Vehicule.objects.prefetch_related("documents")

        recherche = self.request.GET.get("q", "").strip()
        if recherche:
            # Q permet de combiner des conditions avec un OU. icontains est
            # insensible à la casse : chercher « toyota » trouve « Toyota ».
            queryset = queryset.filter(
                Q(immatriculation__icontains=recherche)
                | Q(marque__icontains=recherche)
                | Q(modele__icontains=recherche)
            )

        statut = self.request.GET.get("statut", "")
        if statut:
            queryset = queryset.filter(statut=statut)

        # Par défaut on ne montre que la flotte active : les véhicules sortis
        # restent en base pour leur historique, mais n'encombrent pas l'écran
        # de travail quotidien.
        actif = self.request.GET.get("actif", "1")
        if actif in ("0", "1"):
            queryset = queryset.filter(actif=(actif == "1"))

        return queryset

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)

        # Le statut d'échéance est calculé ici, une fois par ligne affichée, et
        # attaché à l'objet. Un gabarit Django ne peut pas appeler une fonction
        # avec un argument : c'est à la vue de préparer ce qu'il affichera.
        for vehicule in contexte["vehicules"]:
            vehicule.statut_assurance = services.statut_assurance(vehicule)

        contexte["statuts"] = Vehicule.Statut.choices
        contexte["recherche"] = self.request.GET.get("q", "")
        contexte["statut_choisi"] = self.request.GET.get("statut", "")
        contexte["actif_choisi"] = self.request.GET.get("actif", "1")
        return contexte


class VehiculeDetailView(LoginRequiredMixin, DetailView):
    model = Vehicule
    template_name = "fleet/vehicule_detail.html"
    context_object_name = "vehicule"
    queryset = Vehicule.objects.prefetch_related("documents")

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        vehicule = self.object

        contexte["documents"] = services.documents_avec_statut(vehicule)
        contexte["statut_assurance"] = services.statut_assurance(vehicule)
        contexte["statut_visite"] = services.statut_visite_technique(vehicule)
        # La mission en cours donne le chauffeur actuellement affecté : aucun
        # champ ne le stocke, car ce serait une troisième copie d'une
        # information déjà portée par la mission.
        contexte["mission_en_cours"] = (
            services.missions_en_cours(vehicule=vehicule)
            .select_related("chauffeur")
            .first()
        )
        return contexte


class VehiculeCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Vehicule
    form_class = VehiculeForm
    template_name = "fleet/vehicule_form.html"
    success_message = "Le véhicule %(immatriculation)s a été enregistré."

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = "Nouveau véhicule"
        return contexte


class VehiculeUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Vehicule
    form_class = VehiculeForm
    template_name = "fleet/vehicule_form.html"
    success_message = "Le véhicule %(immatriculation)s a été mis à jour."

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = f"Modifier {self.object.immatriculation}"
        return contexte


@login_required
@require_POST
def vehicule_basculer_activation(request, pk):
    """Sort un véhicule de la flotte, ou l'y fait revenir.

    require_POST : une action qui modifie des données ne doit jamais répondre à
    un GET. Sinon le simple chargement du lien — par un robot d'indexation, un
    préchargement de navigateur ou un favori — suffirait à désactiver un
    véhicule.
    """
    vehicule = get_object_or_404(Vehicule, pk=pk)
    try:
        vehicule = services.basculer_activation_vehicule(vehicule)
    except ValidationError as erreur:
        messages.error(request, " ".join(erreur.messages))
    else:
        if vehicule.actif:
            messages.success(
                request, f"{vehicule.immatriculation} est de retour dans la flotte."
            )
        else:
            messages.success(
                request,
                f"{vehicule.immatriculation} est sorti de la flotte active. Son "
                "historique est conservé.",
            )
    return redirect(reverse("fleet:vehicule_detail", args=[vehicule.pk]))


# --- Chauffeurs --------------------------------------------------------------


class ChauffeurListView(ListeFiltrableView):
    model = Chauffeur
    template_name = "fleet/chauffeur_liste.html"
    context_object_name = "chauffeurs"

    def get_queryset(self):
        queryset = Chauffeur.objects.all()

        recherche = self.request.GET.get("q", "").strip()
        if recherche:
            queryset = queryset.filter(
                Q(nom__icontains=recherche)
                | Q(telephone__icontains=recherche)
                | Q(numero_permis__icontains=recherche)
            )

        actif = self.request.GET.get("actif", "1")
        if actif in ("0", "1"):
            queryset = queryset.filter(actif=(actif == "1"))

        # Le « statut » d'un chauffeur, c'est l'état de son permis. Le filtre
        # porte sur la date en base et non sur la propriété permis_expire :
        # une @property n'existe pas en SQL, on ne peut ni filtrer ni trier
        # dessus. La règle de seuil, elle, reste celle de services.py.
        aujourdhui = timezone.localdate()
        horizon = aujourdhui + timedelta(days=services.SEUIL_ALERTE_ECHEANCE_JOURS)
        permis = self.request.GET.get("permis", "")
        if permis == services.ECHEANCE_EXPIREE:
            queryset = queryset.filter(date_expiration_permis__lt=aujourdhui)
        elif permis == services.ECHEANCE_BIENTOT:
            queryset = queryset.filter(
                date_expiration_permis__gte=aujourdhui,
                date_expiration_permis__lte=horizon,
            )
        elif permis == services.ECHEANCE_VALIDE:
            queryset = queryset.filter(date_expiration_permis__gt=horizon)

        return queryset

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        for chauffeur in contexte["chauffeurs"]:
            chauffeur.statut_permis = services.statut_permis(chauffeur)
        contexte["recherche"] = self.request.GET.get("q", "")
        contexte["actif_choisi"] = self.request.GET.get("actif", "1")
        contexte["permis_choisi"] = self.request.GET.get("permis", "")
        return contexte


class ChauffeurDetailView(LoginRequiredMixin, DetailView):
    model = Chauffeur
    template_name = "fleet/chauffeur_detail.html"
    context_object_name = "chauffeur"

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["statut_permis"] = services.statut_permis(self.object)
        contexte["mission_en_cours"] = (
            services.missions_en_cours(chauffeur=self.object)
            .select_related("vehicule")
            .first()
        )
        return contexte


class ChauffeurCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Chauffeur
    form_class = ChauffeurForm
    template_name = "fleet/chauffeur_form.html"
    success_message = "Le chauffeur %(nom)s a été enregistré."

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = "Nouveau chauffeur"
        return contexte


class ChauffeurUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Chauffeur
    form_class = ChauffeurForm
    template_name = "fleet/chauffeur_form.html"
    success_message = "La fiche de %(nom)s a été mise à jour."

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = f"Modifier {self.object.nom}"
        return contexte


@login_required
@require_POST
def chauffeur_basculer_activation(request, pk):
    """Désactive un chauffeur, ou le réactive."""
    chauffeur = get_object_or_404(Chauffeur, pk=pk)
    try:
        chauffeur = services.basculer_activation_chauffeur(chauffeur)
    except ValidationError as erreur:
        messages.error(request, " ".join(erreur.messages))
    else:
        if chauffeur.actif:
            messages.success(request, f"{chauffeur.nom} est de nouveau actif.")
        else:
            messages.success(
                request,
                f"{chauffeur.nom} est désactivé. Son historique de missions est "
                "conservé.",
            )
    return redirect(reverse("fleet:chauffeur_detail", args=[chauffeur.pk]))
