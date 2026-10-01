# BACKLOG — FleetFlow

Tout ce qui a été écarté du périmètre, avec la raison. Rien n'est ici « pour
plus tard » sans motif : un backlog qui ne dit pas pourquoi une idée a été
repoussée finit par être relu comme une liste de manques.

Dernière mise à jour : 01/10/2026 (fin de la phase A — thème).

---

## 1. Décidé pendant l'audit des Jours 2 et 3

### Multi-clients : un déploiement par client
FleetFlow est un produit générique destiné à plusieurs PME, mais aucun modèle
`Client` ou `Organisation` n'existe, et aucune donnée n'est cloisonnée. Une base
de données = un client, avec une instance de l'application par client.

C'est tenable pour les premiers clients et c'est le choix retenu aujourd'hui.
C'est aussi la décision d'architecture la plus coûteuse à reprendre tard : il
faudrait ajouter une clé étrangère `client` sur chaque modèle, filtrer toutes
les requêtes, et se demander à chaque écran si l'utilisateur a le droit de voir
la ligne. **À trancher avant le troisième ou quatrième client, pas après.**

### Saisie des documents depuis l'interface
La liste des documents est en lecture seule : créer ou renouveler une pièce
passe par l'administration Django, où les documents se saisissent directement
depuis la fiche du véhicule (`DocumentInline`). Le cahier des charges du Jour 3
ne demandait qu'une liste ; l'écriture attendra un CRUD complet, avec la
question du renouvellement (créer une nouvelle ligne plutôt que modifier
l'ancienne, pour garder l'historique).

### Pièces jointes des documents
Pas de champ fichier sur `Document`. Un `FileField` impose `MEDIA_ROOT`, un
service de fichiers en production, et surtout un contrôle d'accès : l'adresse
d'un fichier téléversé ne doit pas être devinable par un tiers. C'est un
chantier en soi, pas une case à cocher.

### Planning des missions et chevauchements
Seule une mission au statut `EN_COURS` engage un véhicule ou un chauffeur, et
`creer_mission()` refuse une date de départ future. Une vraie planification —
réserver un camion pour la semaine prochaine, détecter que deux missions
prévues se chevauchent — demande de raisonner sur des intervalles de dates et
de décider ce qui se passe quand une mission déborde. Le statut `PLANIFIEE`
existe déjà dans le modèle pour accueillir ça.

### Annulation d'une mission
Aucun service ne fait passer une mission à `ANNULEE` : le statut existe et le
seed en crée, mais l'interface ne le propose pas. Il faut d'abord décider si
une annulation libère le véhicule (probablement oui) et si elle doit conserver
un motif obligatoire.

### Contraintes en base supplémentaires
Trois `CheckConstraint` ont été posées (kilométrage d'arrivée, date d'arrivée,
litres strictement positifs). D'autres seraient légitimes : `date_expiration`
postérieure à `date_emission` sur `Document`, `prochaine_echeance_km` supérieure
à `km` sur `Entretien`, coût positif. Elles ne sont pas urgentes, les services
les vérifient déjà.

### Index sur les colonnes de date
Aucun index sur `date_fin_assurance` (devenu `Document.date_expiration`),
`date_depart`, `date` des pleins et entretiens, alors que les filtres portent
dessus. À ajouter quand le volume le justifiera : sur une flotte de vingt
véhicules, un index coûte plus en écriture qu'il ne rapporte en lecture.

### Le statut `HORS_SERVICE` et le champ `actif`
Les deux disent « ce véhicule ne roule plus ». Le cadrage retenu (`actif` =
présence dans la flotte, `statut` = disponibilité opérationnelle) est documenté
dans `notes.md`. Supprimer `HORS_SERVICE` pour ne garder que `actif` serait plus
propre, mais c'est une migration et une reprise de données pour un gain
cosmétique.

---

## 2. Jours 4 à 7 (déjà prévus)

### Tableau de bord et indicateurs
Nombre de véhicules disponibles, missions en cours, coût du carburant du mois,
consommation moyenne par véhicule, top 5 des véhicules les plus coûteux. Tout
existe en base ; c'est un travail d'agrégation (`annotate`, `aggregate`) et de
présentation.

### Écran d'alertes
Une page unique rassemblant tout ce qui expire : assurances, visites
techniques, permis, entretiens en retard. La logique existe déjà
(`statut_echeance`, `statut_prochain_entretien`) ; il manque la requête qui
sélectionne les lignes à problème **en SQL** plutôt qu'en Python.

C'est le point technique à connaître : « expiré » et « expire sous 30 jours »
sont calculés en Python par rapport à la date du jour, donc impossibles à
filtrer avec un `filter()`. Deux voies : filtrer sur la date
(`date_expiration__lte=aujourdhui + 30 jours`), ou passer par un `annotate()`.
La liste des documents contourne aujourd'hui le problème en filtrant la page
déjà chargée, ce qui ne marche que parce que les volumes sont faibles.

### Envoi d'e-mails d'alerte
Notifier le gestionnaire des échéances proches. Demande un compte SMTP, une
tâche planifiée (`cron` ou `manage.py` appelé par le système), et une règle
pour ne pas envoyer le même rappel tous les jours.

### Export Excel des missions et des pleins
Un gestionnaire de PME béninoise travaille sur tableur : pouvoir sortir les
missions ou les pleins d'un mois est une demande évidente. **C'est la
justification de la dépendance `openpyxl`**, déjà installée dans le `venv` et
listée dans `requirements.txt`, mais qu'aucun code n'importe encore.

### Suivi GPS
Annoncé comme module payant. Aucun modèle de position n'existe : il faudrait
`PositionGPS` (véhicule, latitude, longitude, horodatage), un moyen de recevoir
les points (API d'un prestataire comme Cartrack ou Orange Fleet), et une carte.
La fiche véhicule est prête à afficher la dernière position connue en texte dès
que le modèle existera.

### Rôles et permissions
Tout utilisateur connecté peut tout faire. Il faudra distinguer au moins le
gestionnaire (tout), le chef de parc (missions, pleins, entretiens) et la
consultation seule. Django fournit les groupes et les permissions ; le travail
est de décider qui peut quoi, pas de l'implémenter.

---

## 3. Dette technique et confort

### ~~Bootstrap servi depuis un CDN~~ — fait le 01/10/2026
Bootstrap, Bootstrap Icons, Instrument Sans et IBM Plex Mono sont rapatriés dans
`static/vendor/` (840 ko). Un test (`RessourcesLocalesTest`) interdit désormais
toute référence à un CDN dans les pages et vérifie que chaque fichier existe sur
le disque : c'est le genre de régression qu'on ne voit qu'une fois la connexion
coupée, donc chez le client.

### ~~Séparateurs de milliers~~ — fait le 01/10/2026
`django.contrib.humanize` est activé (il fait partie de Django, ce n'est pas une
dépendance nouvelle) et `intcomma` sépare les milliers par une espace insécable
(U+00A0) en locale `fr-fr` — vérifié avant application. Tous les kilométrages et
les montants passent par ce filtre.

### `collectstatic` et service des fichiers statiques en production
En développement, `django.contrib.staticfiles` sert `static/` tout seul. En
production il faudra un `collectstatic` et un serveur de fichiers (Nginx, ou
WhiteNoise si on accepte une dépendance de plus). Tant que l'application tourne
en `runserver`, rien à faire.

### Les captures de la maquette ne sont pas versionnées
Le thème a été construit d'après sept captures d'écran fournies en séance, mais
`docs/maquette/` n'existe pas dans le dépôt. La référence visuelle du projet
n'est donc nulle part : à committer pour que la prochaine personne puisse
comparer. Les commentaires du thème et des gabarits y renvoient déjà par ce
chemin.

### Deux entrées de menu passent sur deux lignes
« Tableau de bord » et « Fournisseurs GPS » accompagnés de leur pastille
« bientôt » dépassent les 222 px de la barre latérale. Sans gravité, et le
problème disparaît de lui-même quand la page existe : la pastille s'en va. À
reprendre seulement si une entrée longue reste durablement désactivée.

### Pas de mise en cache
Chaque page recalcule tout. Inutile à cette échelle, à surveiller le jour où un
client aura trois cents véhicules et un tableau de bord d'agrégats.

### Les valeurs des filtres écrites en clair dans les gabarits
`chauffeur_liste.html` et `document_liste.html` contiennent `"expiree"`,
`"bientot"`, `"valide"` en dur, alors que ce sont les constantes
`services.ECHEANCE_*`. Une divergence ne provoquerait aucune erreur, seulement
un filtre sans effet. Un `TextChoices` exposé au gabarit réglerait le problème.

### `seed_demo` et les données d'un client
La commande refuse de tourner avec `DEBUG=False`, ce qui suffit en
développement. Un garde-fou plus fort (confirmation explicite, ou refus si la
base contient des données non créées par le seed) serait prudent le jour où
elle existera sur une machine ayant accès à une base de production.

### Tests de concurrence réelle
`select_for_update()` est utilisé, mais aucun test ne prouve qu'il sérialise
deux écritures simultanées : SQLite ignore l'instruction, et il faudrait deux
connexions sur PostgreSQL pour le vérifier. À faire au moment du passage à
PostgreSQL, avec `TransactionTestCase` et deux fils d'exécution.
