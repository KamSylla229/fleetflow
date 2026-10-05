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
