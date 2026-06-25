"""
auth.py — authentification JWT + gestion des utilisateurs
Corrections apportées :
  - datetime.utcnow() remplacé par datetime.now(timezone.utc) (non déprécié Python 3.12+)
  - Imports tous en haut du fichier
  - Commentaire TODO explicite pour la migration LDAP/AD Orange
"""
import os
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dxc-reclamations-secret-key-2026")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 8

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Utilisateurs hardcodés — valable uniquement en phase locale/démo ──
# TODO (migration Orange) : remplacer par une vérification LDAP/AD via ldap3 :
#   import ldap3
#   server = ldap3.Server("ldap://ad.ocitnetad.ci")
#   conn = ldap3.Connection(server, user=f"OCITNETAD\\{login}", password=password)
#   conn.bind()  →  True si identifiants valides
# Les rôles admin/utilisateur seront alors gérés via les groupes AD.
USERS: dict[str, dict] = {
    "YRPV3142": {
        "nom_complet": "Alassane FOFANA",
        "password": pwd_context.hash("xx"),
        "role": "admin",
    },
    "TCDD2856": {
        "nom_complet": "Agent TCDD2856",
        "password": pwd_context.hash("xxx"),
        "role": "utilisateur",
    },
    "AGENT01": {"nom_complet": "Agent 01", "password": pwd_context.hash("Agent01@"), "role": "utilisateur"},
    "AGENT02": {"nom_complet": "Agent 02", "password": pwd_context.hash("Agent02@"), "role": "utilisateur"},
    "AGENT03": {"nom_complet": "Agent 03", "password": pwd_context.hash("Agent03@"), "role": "utilisateur"},
    "AGENT04": {"nom_complet": "Agent 04", "password": pwd_context.hash("Agent04@"), "role": "utilisateur"},
    "AGENT05": {"nom_complet": "Agent 05", "password": pwd_context.hash("Agent05@"), "role": "utilisateur"},
    "AGENT06": {"nom_complet": "Agent 06", "password": pwd_context.hash("Agent06@"), "role": "utilisateur"},
    "AGENT07": {"nom_complet": "Agent 07", "password": pwd_context.hash("Agent07@"), "role": "utilisateur"},
    "AGENT08": {"nom_complet": "Agent 08", "password": pwd_context.hash("Agent08@"), "role": "utilisateur"},
}


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def authenticate_user(login: str, password: str) -> dict | None:
    user = USERS.get(login.upper())
    if not user:
        return None
    if not verify_password(password, user["password"]):
        return None
    return {
        "login": login.upper(),
        "nom_complet": user["nom_complet"],
        "role": user["role"],
    }


def create_token(user_data: dict) -> str:
    # datetime.now(timezone.utc) — timezone-aware, non déprécié (Python 3.12+)
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "login": user_data["login"],
        "nom_complet": user_data["nom_complet"],
        "role": user_data["role"],
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None