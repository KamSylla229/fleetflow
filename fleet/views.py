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

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.humanize.templatetags.humanize import intcomma
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    RedirectView,
    TemplateView,
    UpdateView,
)

from . import services
from .echeances import classer_echeances, collecter_echeances
from .itineraires import CENTRE_FLOTTE, ZOOM_FLOTTE, itineraire_par_code
from .kpi import calculer_kpi
from .forms import (
    ChauffeurForm,
    ClotureMissionForm,
    EntretienForm,
    MissionForm,
    PleinCarburantForm,
    VehiculeForm,
)
from .models import (
    Alerte,
    Chauffeur,
    Document,
    Entretien,
    Mission,
    PleinCarburant,
    Vehicule,
)


class AccueilView(LoginRequiredMixin, RedirectView):
    """La racine du site mène à la liste des véhicules.

    Elle n'ouvre pas le tableau de bord, bien qu'il existe depuis la phase D :
    changer la destination de l'accueil change l'habitude de tous les
    utilisateurs et casse les favoris. La bascule est notée dans BACKLOG.md,
    pour être décidée pour elle-même et non en passant.
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
        #
        # annoter_dernieres_positions ajoute le dernier relevé GPS de chaque
        # camion par sous-requêtes corrélées : le statut GPS de la colonne ne
        # coûte alors aucune requête supplémentaire.
        queryset = services.annoter_dernieres_positions(
            Vehicule.objects.select_related("fournisseur_gps").prefetch_related(
                "documents"
            )
        )

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

        # Deux requêtes pour toute la page, quel que soit le nombre de lignes :
        # une pour les consommations, une pour les missions en cours. Appeler
        # ces services dans la boucle du gabarit en coûterait deux par ligne.
        vehicules = services.annoter_consommation_moyenne(contexte["vehicules"])
        missions = services.missions_en_cours_par_vehicule(vehicules)

        # Le statut est calculé ici, une fois par ligne, et attaché à l'objet.
        # Un gabarit Django ne peut pas appeler une fonction avec un argument :
        # c'est à la vue de préparer ce qu'il affichera.
        maintenant = timezone.now()
        for vehicule in vehicules:
            vehicule.statut_assurance = services.statut_assurance(vehicule)
            vehicule.statut_operationnel = services.statut_operationnel(vehicule)
            # Le même instant pour toute la page : sinon deux camions au même
            # état pourraient être qualifiés différemment selon la
            # milliseconde à laquelle leur ligne a été calculée.
            vehicule.statut_gps = services.statut_gps(vehicule, maintenant=maintenant)
            vehicule.mission_en_cours = missions.get(vehicule.pk)
        contexte["vehicules"] = vehicules

        # Les indicateurs portent sur la flotte active entière, pas sur la page
        # affichée : « kilométrage cumulé » n'aurait aucun sens page par page.
        indicateurs = services.indicateurs_flotte()
        contexte["indicateurs"] = indicateurs

        # Les lignes d'aide des cartes sont du texte d'interface, assemblé ici
        # plutôt que dans le gabarit, où une condition de plus nuirait à la
        # lecture.
        if indicateurs.annee_plus_ancien:
            contexte["aide_age"] = f"Le plus ancien : {indicateurs.annee_plus_ancien}"
        else:
            contexte["aide_age"] = ""
        contexte["aide_immobilisation"] = ", ".join(indicateurs.immobilises)
        contexte["unite_immobilisation"] = (
            "camions" if len(indicateurs.immobilises) > 1 else "camion"
        )

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
        contexte["statut_operationnel"] = services.statut_operationnel(vehicule)
        contexte["statut_gps"] = services.statut_gps(vehicule)
        contexte["derniere_position"] = services.derniere_position(vehicule)
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

        # Trois requêtes de plus, chacune limitée à dix lignes et accompagnée
        # d'un select_related pour l'objet lié affiché. Les modèles trient déjà
        # du plus récent au plus ancien (ordering dans leur Meta) : le [:10]
        # donne donc bien les dix derniers, et non dix au hasard.
        contexte["missions"] = vehicule.missions.select_related("chauffeur")[:10]
        contexte["pleins"] = services.annoter_consommations(
            vehicule.pleins.select_related("chauffeur")[:10]
        )

        entretiens = list(vehicule.entretiens.all()[:10])
        for entretien in entretiens:
            entretien.statut = services.statut_prochain_entretien(
                entretien, vehicule.kilometrage
            )
        contexte["entretiens"] = entretiens

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

        # Une requête pour toute la page : sans elle, statut_chauffeur()
        # interrogerait la base une fois par ligne pour savoir si le chauffeur
        # est en mission.
        chauffeurs = list(contexte["chauffeurs"])
        missions = services.missions_en_cours_par_chauffeur(chauffeurs)
        for chauffeur in chauffeurs:
            chauffeur.statut_permis = services.statut_permis(chauffeur)
            chauffeur.mission_en_cours = missions.get(chauffeur.pk)
            chauffeur.statut_activite = services.statut_chauffeur(
                chauffeur, mission_en_cours=chauffeur.mission_en_cours
            )
        contexte["chauffeurs"] = chauffeurs

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
        contexte["statut_activite"] = services.statut_chauffeur(self.object)
        contexte["mission_en_cours"] = (
            services.missions_en_cours(chauffeur=self.object)
            .select_related("vehicule")
            .first()
        )
        # Les vingt dernières missions : de quoi juger l'activité récente d'un
        # chauffeur sans charger cinq ans d'historique. L'intégralité reste
        # accessible depuis la liste des missions, filtrée sur ce chauffeur.
        contexte["missions"] = self.object.missions.select_related("vehicule")[:20]
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


# --- Filtres partagés --------------------------------------------------------


def _filtrer_periode(queryset, requete, champ):
    """Applique les filtres de période ?debut=&fin= sur un champ de date.

    Les dates arrivent de la chaîne de requête, donc sous forme de texte, et
    d'une source que l'on ne contrôle pas. parse_date les convertit et renvoie
    None si le format ne correspond pas ; une date bien formée mais inexistante
    (« 2026-02-31 ») lève ValueError. Dans les deux cas on ignore le filtre
    plutôt que de renvoyer une erreur 500 : une adresse bricolée à la main ne
    doit pas casser la page.
    """
    for parametre, operateur in (("debut", "gte"), ("fin", "lte")):
        texte = requete.GET.get(parametre, "")
        if not texte:
            continue
        try:
            valeur = parse_date(texte)
        except ValueError:
            valeur = None
        if valeur is not None:
            queryset = queryset.filter(**{f"{champ}__{operateur}": valeur})
    return queryset


def _contexte_periode(requete):
    """Renvoie les bornes saisies, pour les réafficher dans le formulaire."""
    return {
        "debut": requete.GET.get("debut", ""),
        "fin": requete.GET.get("fin", ""),
    }


# --- Missions ----------------------------------------------------------------


class MissionListView(ListeFiltrableView):
    model = Mission
    template_name = "fleet/mission_liste.html"
    context_object_name = "missions"

    def get_queryset(self):
        # select_related : le véhicule et le chauffeur sont affichés sur chaque
        # ligne. Sans lui, vingt-cinq missions déclencheraient cinquante
        # requêtes de plus.
        queryset = Mission.objects.select_related("vehicule", "chauffeur")

        vehicule = self.request.GET.get("vehicule", "")
        if vehicule.isdigit():
            queryset = queryset.filter(vehicule_id=vehicule)

        chauffeur = self.request.GET.get("chauffeur", "")
        if chauffeur.isdigit():
            queryset = queryset.filter(chauffeur_id=chauffeur)

        statut = self.request.GET.get("statut", "")
        if statut:
            queryset = queryset.filter(statut=statut)

        return _filtrer_periode(queryset, self.request, "date_depart")

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        # Le ton de chaque pastille vient de services.py : le gabarit ne
        # choisit pas qu'une mission annulée se voit en rouge.
        for mission in contexte["missions"]:
            mission.statut_affichable = services.statut_mission(mission)

        contexte.update(_contexte_periode(self.request))
        contexte["vehicules"] = Vehicule.objects.all()
        contexte["chauffeurs"] = Chauffeur.objects.all()
        contexte["statuts"] = Mission.Statut.choices
        contexte["vehicule_choisi"] = self.request.GET.get("vehicule", "")
        contexte["chauffeur_choisi"] = self.request.GET.get("chauffeur", "")
        contexte["statut_choisi"] = self.request.GET.get("statut", "")
        return contexte


class MissionCreateView(LoginRequiredMixin, FormView):
    """Affectation d'une mission, déléguée à services.creer_mission().

    Une FormView et non une CreateView : c'est le service qui crée l'objet, pas
    le formulaire. CreateView appellerait form.save() et court-circuiterait
    toutes les vérifications d'engagement.
    """

    template_name = "fleet/mission_form.html"
    form_class = MissionForm

    def get_initial(self):
        # La date du jour est pré-remplie : c'est le cas courant, et le service
        # refuse de toute façon une date future.
        return {"date_depart": timezone.localdate()}

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = "Nouvelle mission"
        return contexte

    def form_valid(self, form):
        donnees = form.cleaned_data
        try:
            mission = services.creer_mission(
                vehicule=donnees["vehicule"],
                chauffeur=donnees["chauffeur"],
                depart=donnees["depart"],
                destination=donnees["destination"],
                date_depart=donnees["date_depart"],
                km_depart=donnees["km_depart"],
                commentaire=donnees["commentaire"],
            )
        except ValidationError as erreur:
            # form_invalid réaffiche le formulaire avec les valeurs saisies :
            # l'utilisateur n'a pas à tout retaper pour corriger un choix.
            messages.error(self.request, " ".join(erreur.messages))
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"Mission {mission.depart} - {mission.destination} ouverte avec "
            f"{mission.vehicule.immatriculation} et {mission.chauffeur.nom}.",
        )
        return redirect("fleet:mission_liste")


class MissionClotureView(LoginRequiredMixin, FormView):
    """Saisie des relevés de retour, déléguée à services.cloturer_mission()."""

    template_name = "fleet/mission_cloture.html"
    form_class = ClotureMissionForm

    def dispatch(self, request, *args, **kwargs):
        # On charge la mission avant toute chose : les deux méthodes get() et
        # post() en ont besoin, et dispatch() est le seul endroit traversé par
        # les deux.
        self.mission = get_object_or_404(
            Mission.objects.select_related("vehicule", "chauffeur"), pk=kwargs["pk"]
        )
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            "date_arrivee": timezone.localdate(),
            "km_arrivee": self.mission.km_depart,
            "commentaire": self.mission.commentaire,
        }

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["mission"] = self.mission
        contexte["titre"] = (
            f"Clôturer la mission {self.mission.depart} - {self.mission.destination}"
        )
        return contexte

    def form_valid(self, form):
        donnees = form.cleaned_data
        try:
            mission = services.cloturer_mission(
                self.mission,
                date_arrivee=donnees["date_arrivee"],
                km_arrivee=donnees["km_arrivee"],
                commentaire=donnees["commentaire"],
            )
        except ValidationError as erreur:
            messages.error(self.request, " ".join(erreur.messages))
            return self.form_invalid(form)

        messages.success(
            self.request,
            f"Mission clôturée : {mission.distance} km parcourus. Le compteur de "
            f"{mission.vehicule.immatriculation} est à "
            f"{mission.vehicule.kilometrage} km.",
        )
        return redirect("fleet:mission_liste")


# --- Carburant ---------------------------------------------------------------


class PleinListView(ListeFiltrableView):
    model = PleinCarburant
    template_name = "fleet/plein_liste.html"
    context_object_name = "pleins"

    def get_queryset(self):
        queryset = PleinCarburant.objects.select_related("vehicule", "chauffeur")

        vehicule = self.request.GET.get("vehicule", "")
        if vehicule.isdigit():
            queryset = queryset.filter(vehicule_id=vehicule)

        return _filtrer_periode(queryset, self.request, "date")

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        # annoter_consommations complète chaque plein affiché avec sa
        # consommation, en une seule requête pour toute la page. Le calcul a
        # besoin du plein précédent de chaque véhicule, qui peut se trouver sur
        # une autre page de la liste : c'est pourquoi il relit l'historique
        # plutôt que de se contenter des lignes affichées.
        services.annoter_consommations(contexte["pleins"])

        contexte.update(_contexte_periode(self.request))
        contexte["vehicules"] = Vehicule.objects.all()
        contexte["vehicule_choisi"] = self.request.GET.get("vehicule", "")
        return contexte


class PleinCreateView(LoginRequiredMixin, FormView):
    template_name = "fleet/plein_form.html"
    form_class = PleinCarburantForm

    def get_initial(self):
        return {"date": timezone.localdate()}

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = "Nouveau plein de carburant"
        return contexte

    def form_valid(self, form):
        donnees = form.cleaned_data
        try:
            plein, consommation = services.enregistrer_plein(
                vehicule=donnees["vehicule"],
                chauffeur=donnees["chauffeur"],
                date=donnees["date"],
                litres=donnees["litres"],
                prix_litre=donnees["prix_litre"],
                km_compteur=donnees["km_compteur"],
            )
        except ValidationError as erreur:
            messages.error(self.request, " ".join(erreur.messages))
            return self.form_invalid(form)

        cout = plein.cout_total
        if consommation is None:
            # Premier plein du véhicule : le dire explicitement vaut mieux que
            # d'afficher un tiret sans explication.
            messages.success(
                self.request,
                f"Plein enregistré pour {plein.vehicule.immatriculation} : "
                f"{plein.litres} L, {cout:.0f} FCFA. C'est le premier plein de "
                "ce véhicule, la consommation sera calculée au suivant.",
            )
        else:
            messages.success(
                self.request,
                f"Plein enregistré pour {plein.vehicule.immatriculation} : "
                f"{plein.litres} L, {cout:.0f} FCFA, consommation "
                f"{consommation} L/100 km.",
            )
        return redirect("fleet:plein_liste")


# --- Entretiens --------------------------------------------------------------


class EntretienListView(ListeFiltrableView):
    model = Entretien
    template_name = "fleet/entretien_liste.html"
    context_object_name = "entretiens"

    def get_queryset(self):
        queryset = Entretien.objects.select_related("vehicule")

        vehicule = self.request.GET.get("vehicule", "")
        if vehicule.isdigit():
            queryset = queryset.filter(vehicule_id=vehicule)

        type_entretien = self.request.GET.get("type", "")
        if type_entretien:
            queryset = queryset.filter(type_entretien=type_entretien)

        return _filtrer_periode(queryset, self.request, "date")

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)

        # Le badge « à venir / en retard » compare l'échéance kilométrique au
        # compteur actuel du véhicule, déjà chargé par select_related.
        for entretien in contexte["entretiens"]:
            entretien.statut = services.statut_prochain_entretien(
                entretien, entretien.vehicule.kilometrage
            )

        # Les échéances datées, classées par le module echeances : la page
        # n'additionne ni ne trie rien. Les dépassées arrivent en premier
        # parce que classer_echeances les livre dans cet ordre.
        classement = classer_echeances(collecter_echeances())
        contexte["classement"] = classement
        contexte["total_sous_60"] = (
            len(classement["depassees"])
            + len(classement["sous_30_j"])
            + len(classement["sous_60_j"])
        )

        # Le total porte sur l'année civile, pas sur les lignes filtrées : c'est
        # le chiffre que cherche un gérant, et le libellé du tableau le dit.
        contexte["annee"] = timezone.localdate().year
        contexte["cout_annuel"] = services.cout_entretiens_annee(contexte["annee"])

        contexte.update(_contexte_periode(self.request))
        contexte["vehicules"] = Vehicule.objects.all()
        contexte["types"] = Entretien.TypeEntretien.choices
        contexte["vehicule_choisi"] = self.request.GET.get("vehicule", "")
        contexte["type_choisi"] = self.request.GET.get("type", "")
        return contexte


class EntretienCreateView(LoginRequiredMixin, FormView):
    template_name = "fleet/entretien_form.html"
    form_class = EntretienForm

    def get_initial(self):
        return {"date": timezone.localdate()}

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["titre"] = "Nouvel entretien"
        return contexte

    def form_valid(self, form):
        donnees = form.cleaned_data
        try:
            entretien = services.enregistrer_entretien(
                vehicule=donnees["vehicule"],
                type_entretien=donnees["type_entretien"],
                date=donnees["date"],
                km=donnees["km"],
                cout=donnees["cout"],
                prestataire=donnees["prestataire"],
                prochaine_echeance_km=donnees["prochaine_echeance_km"],
            )
        except ValidationError as erreur:
            messages.error(self.request, " ".join(erreur.messages))
            return self.form_invalid(form)

        if entretien.prochaine_echeance_km is None:
            complement = "Aucune échéance kilométrique pour ce type d'intervention."
        else:
            complement = f"Prochaine échéance à {entretien.prochaine_echeance_km} km."
        messages.success(
            self.request,
            f"{entretien.get_type_entretien_display()} enregistrée pour "
            f"{entretien.vehicule.immatriculation}. {complement}",
        )
        return redirect("fleet:entretien_liste")


# --- Documents ---------------------------------------------------------------


class DocumentListView(ListeFiltrableView):
    """Toutes les pièces administratives de la flotte, avec leur état.

    La saisie n'est pas proposée ici : elle passe pour l'instant par
    l'administration Django, où les documents se remplissent directement depuis
    la fiche du véhicule. C'est une limite assumée du périmètre de ce jour,
    inscrite au BACKLOG.
    """

    model = Document
    template_name = "fleet/document_liste.html"
    context_object_name = "documents"

    def get_queryset(self):
        queryset = Document.objects.select_related("vehicule")

        vehicule = self.request.GET.get("vehicule", "")
        if vehicule.isdigit():
            queryset = queryset.filter(vehicule_id=vehicule)

        type_document = self.request.GET.get("type", "")
        if type_document:
            queryset = queryset.filter(type_document=type_document)

        return queryset

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)

        documents = list(contexte["documents"])
        for document in documents:
            document.statut = services.statut_echeance(document.date_expiration)

        # Filtrer sur l'état ne peut pas se faire en SQL : « expiré » et
        # « bientôt expiré » sont calculés par rapport à la date du jour par
        # statut_echeance(), qui n'existe qu'en Python. On filtre donc la page
        # déjà chargée. La conséquence à connaître : ce filtre s'applique après
        # la pagination, il n'agit que sur les lignes de la page courante.
        etat = self.request.GET.get("etat", "")
        if etat == "alerte":
            documents = [
                document for document in documents if document.statut.est_alerte
            ]
        elif etat:
            documents = [
                document for document in documents if document.statut.code == etat
            ]
        contexte["documents"] = documents

        contexte["vehicules"] = Vehicule.objects.all()
        contexte["types"] = Document.TypeDocument.choices
        contexte["vehicule_choisi"] = self.request.GET.get("vehicule", "")
        contexte["type_choisi"] = self.request.GET.get("type", "")
        contexte["etat_choisi"] = etat
        return contexte


# --- Alertes -----------------------------------------------------------------


class AlerteListView(LoginRequiredMixin, TemplateView):
    """Les alertes ouvertes, puis les dernières résolues.

    Une TemplateView et non une ListView : la page montre deux listes de
    natures différentes, et une ListView n'en pagine qu'une.
    """

    template_name = "fleet/alerte_liste.html"

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        contexte["ouvertes"] = services.alertes_ouvertes()
        # Les cinquante dernières résolutions : au-delà, c'est de l'archive
        # dont personne ne se sert, et la page n'a pas à la charger.
        contexte["resolues"] = (
            Alerte.objects.filter(resolue_le__isnull=False)
            .select_related("vehicule", "chauffeur", "document__vehicule")
            .order_by("-resolue_le")[:50]
        )
        return contexte


@login_required
def alerte_bandeau(request):
    """Le fragment des bandeaux, réinterrogé par le script toutes les 30 s.

    Renvoie du HTML et non du JSON : le gabarit du bandeau existe déjà, et le
    script n'a alors qu'à remplacer le contenu d'un conteneur. Produire du
    JSON obligerait à écrire une deuxième fois, en JavaScript, la mise en
    forme que Django sait faire.

    Le context processor fournit déjà `alertes_sans_signal` : cette vue n'a
    rien à calculer.
    """
    return render(request, "partials/_bandeau_alertes.html")


# --- Tableau de bord ---------------------------------------------------------


def _indicateurs_affichables(donnees):
    """Met en forme les cinq indicateurs, pour le gabarit comme pour le JSON.

    Les deux chemins — le rendu initial de la page et le rafraîchissement par
    l'endpoint — passent par cette fonction. C'est ce qui garantit qu'une
    valeur ne change pas de forme au premier rafraîchissement : si le gabarit
    écrivait « 13 584 » et le JavaScript « 13584 », le chiffre sauterait sous
    les yeux de l'utilisateur dix secondes après l'ouverture de la page.

    Les nombres sont donc formatés **ici**, côté serveur. Les reformater en
    français dans le navigateur reviendrait à écrire deux fois la même règle,
    dans deux langages, et à les voir diverger.
    """
    total = donnees["total_camions"]
    en_mouvement = donnees["en_mouvement"]
    en_maintenance = donnees["en_maintenance"]
    consommation = donnees["consommation_moy"]
    depassees = donnees["depassees"]

    return [
        {
            "cle": "en_service",
            "libelle": "Camions en service",
            "valeur": intcomma(donnees["en_service"]),
            "unite": f"/ {total}",
            "aide": (
                f"{en_maintenance} en maintenance"
                if en_maintenance
                else "aucun camion en maintenance"
            ),
            "variante": "",
        },
        {
            "cle": "en_mouvement",
            "libelle": "En mouvement",
            "valeur": intcomma(en_mouvement),
            "unite": "camion" if en_mouvement == 1 else "camions",
            "aide": "d'après le dernier relevé des boîtiers",
            # La carte mise en avant est celle qui bouge : c'est
            # l'information vivante de la page.
            "variante": "avant",
        },
        {
            "cle": "km_du_jour",
            "libelle": "Kilométrage du jour",
            # Arrondi à l'unité : sur une carte, « 13 584 » se lit d'un coup
            # d'œil là où « 13 583,8 » promet une précision que la mesure n'a
            # pas (voir services.kilometres_parcourus).
            "valeur": intcomma(round(donnees["km_du_jour"])),
            "unite": "km",
            "aide": "somme des distances entre relevés GPS",
            "variante": "",
        },
        {
            "cle": "consommation_moy",
            "libelle": "Consommation flotte",
            # Un tiret et jamais zéro : « on ne sait pas encore » n'est pas
            # « la flotte ne consomme rien ».
            "valeur": "—" if consommation is None else intcomma(consommation),
            "unite": "" if consommation is None else "L/100 km",
            "aide": (
                "pas encore deux pleins sur un même camion"
                if consommation is None
                else f"pondérée, {services.FENETRE_CONSOMMATION_JOURS} derniers jours"
            ),
            "variante": "",
        },
        {
            "cle": "echeances_60j",
            "libelle": "Échéances sous 60 jours",
            "valeur": intcomma(donnees["echeances_60j"]),
            "unite": "pièce" if donnees["echeances_60j"] == 1 else "pièces",
            "aide": (
                f"dont {depassees} dépassée{'s' if depassees > 1 else ''}"
                if depassees
                else "aucune dépassée"
            ),
            "variante": "alerte" if depassees else "",
        },
    ]


def _km_par_camion_affichable(donnees):
    """Les barres de kilométrage : plaque, valeur formatée, largeur en pour cent.

    La largeur est calculée ici et non dans le gabarit : un pourcentage est un
    calcul, et {% widthratio %} se relit mal. Elle est relative au camion qui
    a le plus roulé, pour que la barre la plus longue remplisse la ligne — la
    page sert à comparer les camions entre eux, pas à lire des valeurs
    absolues sur une échelle commune.
    """
    lignes = donnees["km_par_camion"]
    maximum = max((km for _vehicule, km in lignes), default=0)
    return [
        {
            "immatriculation": vehicule.immatriculation,
            "url": vehicule.get_absolute_url(),
            "km": intcomma(round(km)),
            "part": round(km / maximum * 100) if maximum else 0,
        }
        for vehicule, km in lignes
    ]


# Nombre d'échéances montrées sur le tableau de bord. Au-delà, un lien renvoie
# vers la page Entretien : un tableau de bord donne l'alerte et l'ordre de
# grandeur, la liste complète a déjà sa page.
ECHEANCES_AFFICHEES = 8


class DashboardView(LoginRequiredMixin, TemplateView):
    """Le tableau de bord : cinq indicateurs, les échéances, les alertes.

    Le rendu initial affiche des valeurs justes — la page n'attend pas le
    premier appel de l'endpoint pour montrer quelque chose, et reste lisible
    si le JavaScript ne tourne jamais. C'est le même dictionnaire qui sert aux
    deux chemins.
    """

    template_name = "fleet/dashboard.html"

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)

        donnees = calculer_kpi()
        classement = donnees["classement"]

        # Dépassées puis sous 30 jours : les deux listes arrivent déjà triées
        # par urgence de classer_echeances(), et les concaténer conserve cet
        # ordre. Aucun tri, aucune comparaison de dates ici.
        urgentes = classement["depassees"] + classement["sous_30_j"]

        contexte["donnees"] = donnees
        contexte["indicateurs"] = _indicateurs_affichables(donnees)
        contexte["km_par_camion"] = _km_par_camion_affichable(donnees)
        contexte["echeances"] = urgentes[:ECHEANCES_AFFICHEES]
        contexte["echeances_restantes"] = max(len(urgentes) - ECHEANCES_AFFICHEES, 0)
        contexte["mesure_le"] = timezone.localtime().strftime("%H:%M:%S")
        return contexte


@login_required
def dashboard_kpi(request):
    """Les indicateurs en JSON, réinterrogés toutes les dix secondes.

    @login_required, et non une vérification écrite dans le corps de la
    fonction : un visiteur anonyme est redirigé vers la connexion et
    **n'obtient aucune donnée**. Un tableau de bord expose la facture de
    carburant et la position des camions d'une PME ; ce n'est pas une page
    publique, et un endpoint JSON oublié est exactement le genre de porte
    qu'on ne voit pas.

    Renvoie du JSON là où le bandeau d'alertes renvoyait un fragment HTML, et
    la raison est l'inverse de celle d'alors : ici le script ne remplace pas
    un bloc entier, mais quelques nombres à l'intérieur de cartes qui
    existent déjà. Remplacer le HTML ferait clignoter toute la page à chaque
    passage.

    Les valeurs sont déjà formatées : voir _indicateurs_affichables.
    """
    donnees = calculer_kpi()
    return JsonResponse(
        {
            "indicateurs": _indicateurs_affichables(donnees),
            "km_par_camion": _km_par_camion_affichable(donnees),
            # L'heure du serveur, affichée sous le titre : elle dit à
            # l'utilisateur que les chiffres sont frais, ce qu'aucun compteur
            # ne sait faire tout seul.
            "mesure_le": timezone.localtime().strftime("%H:%M:%S"),
        }
    )


# --- Carte -------------------------------------------------------------------


def _camions_carte(maintenant=None):
    """La flotte active prête à être placée sur la carte, en deux requêtes.

    Même parti pris que le tableau de bord : une seule fonction traversée par
    le rendu initial de la page et par l'endpoint JSON, pour que les deux
    écrivent exactement les mêmes chaînes. Les nombres sont formatés ici, pas
    dans le navigateur.

    Les camions sans aucun relevé sont renvoyés **avec** les autres, latitude
    et longitude à None. Ils ne peuvent pas être placés sur le fond de carte,
    mais ils doivent apparaître dans la liste latérale : un camion dont le
    boîtier n'a jamais parlé est précisément celui qu'on cherche, et le faire
    disparaître de l'écran serait le pire des affichages.

    Coût : deux requêtes, quelle que soit la taille de la flotte — la flotte
    annotée de son dernier relevé, et les missions en cours.
    """
    maintenant = maintenant or timezone.now()

    vehicules = list(
        services.annoter_dernieres_positions(
            Vehicule.objects.filter(actif=True).select_related("fournisseur_gps")
        )
    )
    missions = services.missions_en_cours_par_vehicule(vehicules)

    camions = []
    for vehicule in vehicules:
        statut = services.statut_gps(vehicule, maintenant=maintenant)
        mission = missions.get(vehicule.pk)
        horodatage = vehicule.gps_horodatage
        vitesse = vehicule.gps_vitesse_kmh

        camions.append(
            {
                "id": vehicule.pk,
                "immatriculation": vehicule.immatriculation,
                "url": vehicule.get_absolute_url(),
                # float() et non Decimal : JsonResponse ne sait pas sérialiser
                # un Decimal, et une coordonnée n'a pas besoin de la précision
                # exacte du décimal — contrairement à de l'argent.
                "lat": float(vehicule.gps_latitude)
                if vehicule.gps_latitude is not None
                else None,
                "lon": float(vehicule.gps_longitude)
                if vehicule.gps_longitude is not None
                else None,
                "statut": statut.code,
                "statut_libelle": statut.libelle,
                # Le même ton que partout ailleurs dans le thème : la couleur
                # du point sur la carte est celle du badge de la liste des
                # camions. Un vert qui voudrait dire deux choses différentes
                # selon la page serait pire que pas de couleur du tout.
                "ton": statut.ton,
                "vitesse": round(vitesse) if vitesse is not None else None,
                # Une heure absolue, et non « il y a 4 minutes » : la liste est
                # reconstruite toutes les dix secondes, et une durée relative
                # obligerait à écrire son formatage une seconde fois en
                # JavaScript.
                "vu_le": timezone.localtime(horodatage).strftime("%H:%M")
                if horodatage is not None
                else None,
                # Un signal coupé est une décision — l'interrupteur de
                # démonstration —, pas une panne. Les distinguer à l'écran
                # évite de chercher une panne qu'on a provoquée soi-même.
                "signal_coupe": vehicule.signal_coupe,
                "itineraire": vehicule.itineraire,
                "chauffeur": mission.chauffeur.nom if mission else None,
                "destination": mission.destination if mission else None,
                "progression": vehicule.progression_pourcent,
            }
        )

    return camions


def _compteurs_carte(camions):
    """Combien de camions dans chacun des quatre états GPS.

    Sert la ligne sous le titre. Compté ici et non dans le gabarit : quatre
    {% if %} dans une boucle donneraient quatre compteurs à maintenir.
    """
    compteurs = {code: 0 for code in services.LIBELLES_STATUT_GPS}
    for camion in camions:
        compteurs[camion["statut"]] += 1
    return compteurs


def _itineraires_carte(camions):
    """Les polylignes des axes effectivement parcourus par la flotte.

    **Zéro requête.** Les codes d'itinéraire viennent de la liste de camions
    déjà chargée, et `itineraire_par_code` lit le dictionnaire de
    fleet/itineraires.py, pas la base. C'est la leçon de la septième requête
    du tableau de bord, appliquée d'avance : ne jamais recharger ce qu'on a
    déjà en main.

    Ces tracés ne changent jamais : ils sont rendus **une fois** avec la page,
    et l'endpoint de rafraîchissement ne les renvoie pas. Les réexpédier
    toutes les dix secondes serait payer une donnée figée au prix d'une donnée
    vivante.

    Seuls les axes d'au moins un camion actif sont envoyés : afficher les
    trois alors que la flotte n'en suit qu'un encombrerait la carte sans rien
    apprendre.
    """
    codes = {camion["itineraire"] for camion in camions if camion["itineraire"]}
    traces = []
    for code in sorted(codes):
        itineraire = itineraire_par_code(code)
        if itineraire is None:
            continue
        traces.append(
            {
                "code": itineraire.code,
                "libelle": itineraire.libelle,
                "points": [[latitude, longitude] for latitude, longitude in itineraire.points],
            }
        )
    return traces


class CarteView(LoginRequiredMixin, TemplateView):
    """La carte de la flotte : un point par camion, coloré par son état GPS.

    La page est lisible sans qu'une ligne de JavaScript ne tourne : la liste
    latérale est rendue par Django, avec les positions, les vitesses et les
    heures de relevé. Leaflet n'ajoute que le fond de carte — et il est servi
    depuis static/vendor, comme Bootstrap.
    """

    template_name = "fleet/carte.html"

    def get_context_data(self, **kwargs):
        contexte = super().get_context_data(**kwargs)
        camions = _camions_carte()
        contexte["camions"] = camions
        contexte["compteurs"] = _compteurs_carte(camions)
        contexte["itineraires"] = _itineraires_carte(camions)
        contexte["centre"] = list(CENTRE_FLOTTE)
        contexte["zoom"] = ZOOM_FLOTTE
        contexte["mesure_le"] = timezone.localtime().strftime("%H:%M:%S")
        contexte["seuil_minutes"] = settings.FLEETFLOW_SEUIL_SANS_SIGNAL_MIN
        # Vide par defaut : voir le commentaire de FLEETFLOW_TUILES_URL dans
        # config/settings.py. La page sait se passer de fond de carte.
        contexte["tuiles_url"] = settings.FLEETFLOW_TUILES_URL
        contexte["tuiles_attribution"] = settings.FLEETFLOW_TUILES_ATTRIBUTION
        return contexte


@login_required
def carte_positions(request):
    """Les positions de la flotte en JSON, réinterrogées toutes les dix secondes.

    Ne renvoie **pas** les tracés d'itinéraires, ni le centre, ni le zoom :
    tout cela est figé et déjà dans la page. Le rafraîchissement ne transporte
    que ce qui bouge.

    Et surtout, il ne transporte aucune instruction de cadrage. Le script
    recadre la carte une seule fois, au premier affichage : si l'endpoint
    imposait un centre à chaque passage, la carte sauterait toutes les dix
    secondes sous le doigt de l'utilisateur en train de la déplacer.
    """
    camions = _camions_carte()
    return JsonResponse(
        {
            "camions": camions,
            "compteurs": _compteurs_carte(camions),
            "mesure_le": timezone.localtime().strftime("%H:%M:%S"),
        }
    )
