"""Les échéances datées de la flotte : collecte, puis classement.

Deux fonctions, séparées à dessein :

- `collecter_echeances()` **lit** la base et ne décide rien ;
- `classer_echeances()` **décide** et ne lit rien.

Cette coupure n'est pas de la cérémonie. Le classement est la partie qui
contient les règles — la borne des trente jours, celle des soixante, le fait
qu'une pièce est dépassée *le lendemain* de sa date — et c'est donc la partie
qu'il faut pouvoir éprouver sans base de données, sans fixture, et avec des
dates choisies à la main. Ses tests construisent des objets en trois lignes et
tournent en quelques millisecondes.

Ce que ce module ne traite pas, volontairement :

- **les entretiens**, dont l'échéance est kilométrique et non datée. Mélanger
  « dépassé de 17 jours » et « dépassé de 1 000 km » dans la même colonne
  obligerait le lecteur à vérifier l'unité de chaque ligne ;
- **les camions sans aucune attestation**. Une pièce absente n'a pas de date,
  donc pas d'échéance : ce n'est pas un retard, c'est un dossier incomplet.
  C'est un indicateur à part, inscrit au BACKLOG.
"""

from dataclasses import dataclass
from datetime import date

# Les trois familles nommées par le cahier des charges. `kind` peut aussi
# porter les autres types de document (licence de transport, autre) : ce sont
# les valeurs de Document.TypeDocument, reprises telles quelles pour qu'aucune
# échéance réelle ne soit ignorée en silence.
ASSURANCE = "assurance"
VISITE_TECHNIQUE = "visite_technique"
PERMIS = "permis"

# Une pièce est signalée « proche » sous ce nombre de jours, et « lointaine »
# sous celui-là. Au-delà, elle n'apparaît pas.
SEUIL_PROCHE_JOURS = 30
SEUIL_LOINTAIN_JOURS = 60


@dataclass(frozen=True)
class Echeance:
    """Une date limite, détachée de la base.

    Un objet simple, construit par la collecte et consommé par le classement.
    Il porte des références aux objets Django (pour pouvoir afficher une
    plaque ou un nom), mais aucun comportement qui interroge la base : tout ce
    qu'il expose est déjà en mémoire.

    `frozen=True` : une échéance est un constat, personne ne doit pouvoir la
    modifier en cours de classement.

    Une seule des trois références est renseignée à la fois — `document` pour
    une pièce de camion (avec `vehicule` rempli aussi, pour l'affichage),
    `chauffeur` pour un permis.
    """

    kind: str
    date_echeance: date
    vehicule: object = None
    chauffeur: object = None
    document: object = None

    @property
    def libelle(self):
        """« Visite technique — AB 1234 RB », « Permis — Jean Kouassi »."""
        if self.document is not None:
            return (
                f"{self.document.get_type_document_display()} — "
                f"{self.vehicule.immatriculation}"
            )
        if self.chauffeur is not None:
            return f"Permis — {self.chauffeur.nom}"
        return "Échéance"

    @property
    def url(self):
        """La fiche où aller régler le problème."""
        if self.chauffeur is not None:
            return self.chauffeur.get_absolute_url()
        if self.vehicule is not None:
            return self.vehicule.get_absolute_url()
        return ""

    def jours_avant(self, today):
        """Jours restants, négatif si la date est passée."""
        return (self.date_echeance - today).days


@dataclass(frozen=True)
class EcheanceClassee:
    """Une échéance accompagnée du nombre de jours qui la sépare d'aujourd'hui.

    Le nombre de jours ne peut pas vivre sur `Echeance` : il dépend de la date
    du jour, qui n'est connue qu'au classement. Le stocker à la collecte
    donnerait une valeur périmée dès le lendemain — la même raison qui fait de
    `jours_avant_fin_assurance` une propriété et non un champ.
    """

    echeance: Echeance
    jours: int

    # Raccourcis explicites plutôt qu'un __getattr__ qui ferait suivre tous
    # les attributs : trois lignes lisibles valent mieux qu'une indirection
    # magique que personne ne retrouve à la relecture.
    @property
    def kind(self):
        return self.echeance.kind

    @property
    def vehicule(self):
        return self.echeance.vehicule

    @property
    def chauffeur(self):
        return self.echeance.chauffeur

    @property
    def document(self):
        return self.echeance.document

    @property
    def date_echeance(self):
        return self.echeance.date_echeance

    @property
    def libelle(self):
        return self.echeance.libelle

    @property
    def url(self):
        return self.echeance.url

    @property
    def depassee(self):
        return self.jours < 0

    @property
    def detail(self):
        """La ligne secondaire affichée sous le libellé."""
        if self.depassee:
            return f"échue le {self.date_echeance:%d/%m/%Y}"
        return f"échéance le {self.date_echeance:%d/%m/%Y}"


def collecter_echeances(vehicules=None, chauffeurs=None):
    """Lit la base et construit la liste des échéances datées. Lecture seule.

    Deux sources : les pièces **en vigueur** des camions actifs, et les permis
    des chauffeurs actifs.

    « En vigueur » est la subtilité de cette fonction. Un camion dont
    l'assurance a été renouvelée porte deux lignes : l'ancienne, périmée, et la
    nouvelle, valide. Faire remonter l'ancienne afficherait une alerte
    permanente et fausse. On ne garde donc, par couple (camion, type de
    pièce), que l'échéance la plus lointaine — exactement la règle de
    `Vehicule._dernier_document`.

    Les pièces sans `date_expiration` (la carte grise) sont ignorées : une
    pièce qui n'expire pas n'a pas d'échéance.

    Le tri se fait en Python sur `documents.all()` préchargé, et non par un
    `.filter()` SQL : c'est ce qui permet de ne payer que deux requêtes pour
    toute la flotte, et c'est le même choix que dans le modèle.

    Coût : trois requêtes, quelle que soit la taille de la flotte.
    """
    # Les imports sont locaux : ce module est importé par services.py, lui-même
    # importé par models.py en bout de chaîne. Les remonter en tête créerait un
    # import circulaire au démarrage de Django.
    from fleet.models import Chauffeur, Vehicule

    if vehicules is None:
        vehicules = Vehicule.objects.filter(actif=True).prefetch_related("documents")
    if chauffeurs is None:
        chauffeurs = Chauffeur.objects.filter(actif=True)

    echeances = []

    for vehicule in vehicules:
        # Par type de pièce, on retient celle dont l'échéance est la plus
        # lointaine.
        en_vigueur = {}
        for document in vehicule.documents.all():
            if document.date_expiration is None:
                continue
            connu = en_vigueur.get(document.type_document)
            if connu is None or document.date_expiration > connu.date_expiration:
                en_vigueur[document.type_document] = document

        for document in en_vigueur.values():
            echeances.append(
                Echeance(
                    kind=document.type_document,
                    date_echeance=document.date_expiration,
                    vehicule=vehicule,
                    document=document,
                )
            )

    for chauffeur in chauffeurs:
        echeances.append(
            Echeance(
                kind=PERMIS,
                date_echeance=chauffeur.date_expiration_permis,
                chauffeur=chauffeur,
            )
        )

    return echeances


def classer_echeances(echeances, today=None):
    """Range des échéances en dépassées, sous 30 jours et sous 60 jours.

    Fonction **pure** : aucune requête, aucune écriture, aucun accès à l'heure
    système si `today` est fourni. C'est ce qui la rend testable avec des dates
    choisies à la main.

    La règle de bord vient du cahier des charges : une échéance est dépassée
    **le lendemain** de sa date. Une assurance qui expire aujourd'hui couvre
    encore la journée — elle est donc « sous 30 jours », à zéro jour, et pas
    dépassée.

    Chaque liste est triée par urgence croissante : la plus en retard d'abord
    pour les dépassées, la plus proche d'abord pour les deux autres. Un simple
    tri croissant sur le nombre de jours suffit, puisque les retards sont
    négatifs.

    Les échéances au-delà de soixante jours ne sont dans aucune liste : ce sont
    celles dont il n'y a rien à faire aujourd'hui.
    """
    if today is None:
        # Import local pour garder la fonction utilisable sans Django quand
        # l'appelant fournit `today` — ce que font tous ses tests.
        from django.utils import timezone

        today = timezone.localdate()

    classees = [
        EcheanceClassee(echeance=echeance, jours=echeance.jours_avant(today))
        for echeance in echeances
    ]

    depassees = sorted(
        (item for item in classees if item.jours < 0), key=lambda item: item.jours
    )
    sous_30 = sorted(
        (item for item in classees if 0 <= item.jours <= SEUIL_PROCHE_JOURS),
        key=lambda item: item.jours,
    )
    sous_60 = sorted(
        (
            item
            for item in classees
            if SEUIL_PROCHE_JOURS < item.jours <= SEUIL_LOINTAIN_JOURS
        ),
        key=lambda item: item.jours,
    )

    return {"depassees": depassees, "sous_30_j": sous_30, "sous_60_j": sous_60}
