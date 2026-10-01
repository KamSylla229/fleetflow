"""Données injectées dans tous les gabarits.

Un context processor est une fonction appelée à chaque rendu : ce qu'elle
renvoie est disponible dans tous les gabarits, sans qu'aucune vue n'ait à le
passer. C'est le bon outil pour ce qui est vrai partout — ici, l'élément de
menu actif. L'alternative (un `nav_actif` ajouté dans chaque
get_context_data) ferait trente endroits à maintenir au lieu d'un.

À utiliser avec parcimonie : une requête SQL posée ici est payée sur *chaque*
page du site. Celui-ci n'en fait aucune.
"""

# Le nom de la route courante commence par le nom de l'objet
# (« vehicule_liste », « mission_cloturer »). On s'en sert pour deviner
# l'onglet, au lieu d'énumérer les trente routes du projet.
PREFIXES_NAVIGATION = (
    ("vehicule", "camions"),
    ("chauffeur", "chauffeurs"),
    ("mission", "missions"),
    ("plein", "carburant"),
    ("entretien", "entretien"),
    ("document", "documents"),
)


def initiales(utilisateur):
    """Deux lettres pour la pastille du pied de sidebar.

    Calculé ici et non dans le gabarit : enchaîner `first`, `upper` et
    `default` sur deux champs donne une ligne illisible que personne ne
    saurait relire, et qu'aucun test ne pourrait viser.
    """
    if utilisateur is None or not utilisateur.is_authenticated:
        return ""

    prenom = (utilisateur.first_name or "").strip()
    nom = (utilisateur.last_name or "").strip()
    if prenom and nom:
        return (prenom[0] + nom[0]).upper()

    # Pas de nom complet : on prend les deux premières lettres de
    # l'identifiant, ce qui donne « DE » pour le compte de démonstration.
    base = prenom or nom or utilisateur.get_username()
    return base[:2].upper()


def navigation(request):
    """Entrée de menu active et initiales de l'utilisateur connecté."""
    correspondance = getattr(request, "resolver_match", None)
    nom_route = getattr(correspondance, "url_name", "") or ""

    nav_actif = ""
    for prefixe, cle in PREFIXES_NAVIGATION:
        if nom_route.startswith(prefixe):
            nav_actif = cle
            break

    return {
        "nav_actif": nav_actif,
        "initiales_utilisateur": initiales(getattr(request, "user", None)),
    }
