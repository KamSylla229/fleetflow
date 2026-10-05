# BACKLOG — FleetFlow

Tout ce qui a été écarté du périmètre, avec la raison. Rien n'est ici « pour
plus tard » sans motif : un backlog qui ne dit pas pourquoi une idée a été
repoussée finit par être relu comme une liste de manques.

Dernière mise à jour : 05/10/2026 (fin de la phase C — alertes, échéances, rapport).

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

### ~~Écran d'alertes~~ — fait les 01 et 05/10/2026
La page `/alertes/` liste les alertes ouvertes puis résolues, et la page
Entretien range les échéances datées en trois colonnes. Les entretiens en
retard gardent leur badge kilométrique dans leur propre tableau : voir plus
bas « unifier les deux natures d'échéance ».

C'est le point technique à connaître : « expiré » et « expire sous 30 jours »
sont calculés en Python par rapport à la date du jour, donc impossibles à
filtrer avec un `filter()`. Deux voies : filtrer sur la date
(`date_expiration__lte=aujourdhui + 30 jours`), ou passer par un `annotate()`.
La liste des documents contourne aujourd'hui le problème en filtrant la page
déjà chargée, ce qui ne marche que parce que les volumes sont faibles.

### ~~Envoi d'e-mails d'alerte~~ — fait le 05/10/2026
`verifier_signaux` et `verifier_echeances` préviennent le gérant une seule
fois par problème — c'est le rôle du champ `email_envoye_le` et des trois
contraintes d'unicité partielles. `rapport_quotidien --email` envoie le
récapitulatif. Reste à brancher la tâche planifiée, ci-dessous.

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

### Tâche planifiée pour le rapport quotidien
`rapport_quotidien --email` est prêt et n'envoie rien sans cette option. Il
reste à l'appeler une fois par jour depuis l'hébergeur (un *cron job* Render,
ou l'équivalent). À décider en même temps : l'heure d'envoi, et si le rapport
doit porter sur la journée écoulée (`--date` de la veille) plutôt que sur la
journée en cours, ce qui est plus logique pour un envoi du matin.

### Indicateur : assurance manquante
Un camion sans aucune attestation d'assurance **n'apparaît pas** dans les
trois colonnes d'échéances et ne déclenche aucune alerte. C'est voulu : une
pièce absente n'a pas de date, donc pas d'échéance — ce n'est pas un retard,
c'est un dossier incomplet.

Mais c'est un problème au moins aussi grave qu'une assurance expirée, et
aujourd'hui il ne se voit que sur la fiche du camion, par le badge
« Aucune attestation ». Il faudrait un indicateur à part — « N camions sans
assurance enregistrée » — avec sa propre liste. Les briques existent :
`services.statut_assurance()` renvoie déjà le code `ECHEANCE_ABSENTE`, et
`est_alerte` le compte comme une alerte.

### Unifier les deux natures d'échéance
Les échéances datées (pièces, permis) et kilométriques (entretiens) vivent
dans deux endroits distincts de la page Entretien, parce que « dépassé de
17 jours » et « dépassé de 1 000 km » ne se comparent pas. Une vraie
unification demanderait de convertir les kilomètres en jours à partir du
rythme d'usage du camion — faisable à partir des positions GPS, mais c'est un
modèle de prévision, pas un affichage.

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

### Faire du tableau de bord la page d'accueil
`AccueilView` redirige vers la liste des camions, décidé quand le tableau de
bord n'existait pas. Maintenant qu'il existe, c'est probablement lui qu'un
gérant veut voir en arrivant. Pas changé en passant : l'adresse d'accueil est
l'habitude de tous les utilisateurs et le favori qu'ils ont posé. À décider
pour elle-même, avec le client.

### Filtrer le tableau de bord par période
La maquette `01-dashboard.png` porte trois boutons « Jour / 7 jours / Mois » et
un bouton « Exporter ». `calculer_kpi()` ne connaît qu'une date de référence :
les kilomètres sont ceux du jour, et la consommation ceux des trente derniers
jours, deux fenêtres figées. Les rendre réglables demande de passer la fenêtre
à `kilometres_parcourus` comme on l'a fait pour `_cumuls_carburant`, et de
décider ce que « en service » veut dire sur une semaine — un camion disponible
aujourd'hui l'était-il lundi ? L'historique des statuts n'existe pas, donc la
réponse est non, et c'est la vraie difficulté de ce point.

### L'entrée « Tableau de bord » du menu ne déborde plus
Le point « Deux entrées de menu passent sur deux lignes » ci-dessus est résolu
pour moitié : la pastille « bientôt » a disparu de cette entrée en phase D,
comme prévu. Reste « Fournisseurs GPS ».

### Les plaques se chevauchent quand les camions sont au même endroit
Huit camions au départ de Cotonou donnent huit plaques empilées sur le dessin.
Ce n'est plus le défaut d'une bibliothèque — il n'y en a plus — mais celui de
l'affichage d'étiquettes en général : il faudrait les décaler les unes par
rapport aux autres, ou n'afficher que celle du camion choisi. À décider devant
une flotte réelle, et à l'œil, pas en principe.

### Rejouer le trajet de la journée
La carte montre la dernière position connue. Voir le chemin parcouru depuis ce
matin demanderait de renvoyer l'historique des relevés et de le dessiner, avec
un curseur de temps. Volontairement hors cadre de la phase D : l'écran doit
d'abord répondre à « où sont mes camions maintenant ».
