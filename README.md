# FleetFlow — gestion de flotte pour les PME béninoises

Les PME du Bénin suivent leurs véhicules sur des cahiers et des tableurs : assurances expirées sans prévenir, entretiens non tracés, consommation de carburant invérifiable. FleetFlow centralise véhicules, chauffeurs, missions et coûts dans une seule base de données.

- **MVP** — véhicules, chauffeurs, missions, entretiens, pleins de carburant et documents administratifs. Interface web dédiée (Bootstrap 5) pour les véhicules et les chauffeurs, admin Django pour le reste ; échéances d'assurance, de visite technique et de permis calculées par rapport à la date du jour, avec badges vert / orange / rouge.
- **Règles de gestion** — toutes dans `fleet/services.py` : un véhicule ou un chauffeur déjà engagé sur une mission en cours ne peut pas être affecté à une autre, la clôture d'une mission reporte les kilomètres sur le véhicule, la consommation se calcule entre deux pleins. Couvertes par `python manage.py test`.
- **Stack** — Python 3.11, Django 5.2, SQLite, django-environ. Bootstrap 5, Bootstrap Icons, Instrument Sans, IBM Plex Mono et Leaflet 1.9.4 sont **servis en local** depuis `static/vendor/` : l'application garde son style sans connexion Internet. Seules les tuiles de la carte viennent du réseau (OpenStreetMap) — sans elles, la page Carte affiche toujours les camions et les itinéraires, sur un fond vide. `openpyxl` est installé d'avance pour l'export Excel prévu au backlog, mais aucun code ne l'utilise encore.
- **Interface** — thème maison dans `static/css/fleetflow.css` (jetons de couleur, de rayon et de police), barre latérale fixe, pastilles de statut vert / ambre / rouge / neutre. Un seul gabarit de pastille, piloté par le *ton* que renvoient les services : aucun gabarit ne choisit une couleur.
- **Suivi GPS** — positions simulées par `simuler_positions` en attendant un vrai boîtier ; statut calculé (en route / à l'arrêt / sans signal / aucune donnée), alertes de boîtier muet et d'échéance, rapport quotidien par e-mail.
- **À venir** — tableau de bord, carte Leaflet, export Excel. Voir `BACKLOG.md`.

**Installation locale** (Windows, invite de commandes) :
```bash
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt
copy .env.example .env                 # puis renseigner SECRET_KEY
python manage.py migrate && python manage.py seed_demo    # base + données de démo
python manage.py runserver
```

Le jeu de démonstration crée le compte **`demo` / `demo1234`** (uniquement si `DEBUG=True`).
`seed_demo` efface toutes les données FleetFlow avant de les recréer : elle refuse de
tourner avec `DEBUG=False`.

**Suivre la flotte et les alertes** :
```bash
python manage.py simuler_positions --boucle     # positions GPS en continu
python manage.py simuler_positions --couper "AB 1234 RB"   # boîtier muet
python manage.py rapport_quotidien              # affiche le rapport du jour
python manage.py rapport_quotidien --email      # et l'envoie au gérant
```

Par défaut, aucun e-mail ne part : `EMAIL_BACKEND` vaut `console`, et
`rapport_quotidien` se contente d'afficher. Pour voir une alerte apparaître en
démonstration, mettez `FLEETFLOW_SEUIL_SANS_SIGNAL_MIN=1` dans le `.env`,
sinon il faut attendre trente minutes.

**Lancer les tests** :
```bash
python manage.py test          # 321 tests
```

**Vérifier que l'application fonctionne hors connexion** : coupez le Wi-Fi et
rechargez une page — le style, les icônes et les polices doivent rester en
place. Le test `RessourcesLocalesTest` garde cette propriété automatiquement.
