# FleetFlow — gestion de flotte pour les PME béninoises

Les PME du Bénin suivent leurs véhicules sur des cahiers et des tableurs : assurances expirées sans prévenir, entretiens non tracés, consommation de carburant invérifiable. FleetFlow centralise véhicules, chauffeurs, missions et coûts dans une seule base de données.

- **MVP** — véhicules, chauffeurs, missions, entretiens et pleins de carburant, gérés depuis l'admin Django ; échéances d'assurance, de visite technique et de permis calculées par rapport à la date du jour (alertes d'échéance à venir).
- **Stack** — Python 3.11, Django 5.2, SQLite, django-environ. Aucune autre dépendance.
- **À venir** — le suivi GPS en temps réel est prévu comme module payant.

**Installation locale** (Windows, invite de commandes) :
```bash
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt
copy .env.example .env                 # puis renseigner SECRET_KEY
python manage.py migrate && python manage.py seed_demo    # base + données de démo
python manage.py runserver
```
