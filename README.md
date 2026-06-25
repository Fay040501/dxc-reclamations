# DXC Réclamations — Back-office

Application web interne de gestion et de traitement des réclamations clients,
développée pour les équipes back-office Orange/DXC.

## Stack technique

- **Backend** : Python 3.12 · FastAPI · psycopg2 (PostgreSQL)
- **Frontend** : React 18 (Babel standalone) · SVG natifs · CSS variables
- **Auth** : JWT · bcrypt · sessions persistées en base
- **DB** : PostgreSQL — table `tb_reclamations` + `dxc_active_sessions`

## Fonctionnalités

### Espace Admin
- **Vue d'ensemble** : KPIs temps réel (total, non assignées, assignées, traitées)
  avec pagination serveur (100 par page)
- **Dispatch** : attribution par catégorie ou motif de non-paiement,
  multi-sélection, volume exact depuis la base (toutes pages)
- **Corbeille** : volume par agent avec taux de traitement, redistribution
  entre agents par motif, export CSV
- **Dashboard** : taux global, évolution quotidienne, volume par motif

### Espace Agent (Utilisateur)
- **Mes réclamations** : file À traiter / Traitées avec filtres date
- **Formulaire de traitement** : statut appel, niveau, sous-motif,
  motif réel, zone client, ID dossier, commentaire
- **Dashboard** : progression personnelle, répartition N1/N2

### Sécurité
- Session unique par login (double connexion invalide la précédente)
- Rate limiting sur `/login` (5 tentatives / 60s par IP)
- Cookie `httponly` + `samesite=lax`
- Timer d'inactivité 2h côté client avec refresh token automatique
- Whitelist colonnes SQL (prévention injection)

## Installation

```bash
pip install fastapi "uvicorn[standard]" psycopg2-binary \
    python-jose[cryptography] passlib[bcrypt] \
    python-dotenv jinja2 python-multipart
```

## Configuration

Créer un fichier `.env` à la racine :

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=nom_de_la_base
DB_USER=utilisateur_pg
DB_PASSWORD=mot_de_passe
DB_SSLMODE=prefer
SECRET_KEY=cle-secrete-longue-et-aleatoire
```

## Lancement

```bash
# Développement
uvicorn main:app --reload --port 8000

# Production
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Comptes par défaut

| Login | Rôle | Mot de passe |
|---|---|---|
| YRPV3142 | Admin | xx |
| TCDD2856 | Agent | xx |
| AGENT01–08 | Agent | xx |

> ⚠️ Changer les mots de passe avant tout déploiement en production.

## Structure

```
├── main.py          # Routes FastAPI + logique métier
├── auth.py          # JWT + hachage bcrypt + utilisateurs
├── database.py      # Pool PostgreSQL + sessions persistantes
├── templates/
│   ├── app.html     # SPA React (Admin + Agent)
│   └── login.html   # Page de connexion
└── static/
    └── styles.css   # Design system Orange (--accent: #FF7900)
```

---

## Auteur

**Fofana Alassane Yahaya**
Data Analyst — Direction de l'Expérience Client, Orange Côte d'Ivoire
Master 2 Ingénierie Statistique et Data Science — INSSEDS

[GitHub](https://github.com/Fay040501) · [LinkedIn](https://www.linkedin.com/in/fofana-alassane-yahaya)