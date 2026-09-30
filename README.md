# FleetFlow — gestion de flotte pour les PME béninoises

Les PME du Bénin suivent leurs véhicules sur des cahiers et des tableurs : assurances expirées sans prévenir, entretiens non tracés, consommation de carburant invérifiable. FleetFlow centralise véhicules, chauffeurs, missions et coûts dans une seule base de données.

- **MVP** — véhicules, chauffeurs, missions, entretiens, pleins de carburant et documents administratifs. Interface web dédiée (Bootstrap 5) pour les véhicules et les chauffeurs, admin Django pour le reste ; échéances d'assurance, de visite technique et de permis calculées par rapport à la date du jour, avec badges vert / orange / rouge.
- **Règles de gestion** — toutes dans `fleet/services.py` : un véhicule ou un chauffeur déjà engagé sur une mission en cours ne peut pas être affecté à une autre, la clôture d'une mission reporte les kilomètres sur le véhicule, la consommation se calcule entre deux pleins. Couvertes par `python manage.py test`.
- **Stack** — Python 3.11, Django 5.2, SQLite, django-environ ; Bootstrap 5 par CDN. `openpyxl` est installé d'avance pour l'export Excel prévu au backlog, mais aucun code ne l'utilise encore.
- **À venir** — le suivi GPS en temps réel est prévu comme module payant.

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

**Lancer les tests** :
```bash
python manage.py test
```
