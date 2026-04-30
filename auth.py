from passlib.context import CryptContext
from datetime import datetime, timedelta
from dotenv import load_dotenv
import jwt
import os

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dxc-reclamations-secret-key-2026")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 8

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "YRPV3142": {
        "nom_complet": "Alassane FOFANA",
        "password": pwd_context.hash("Mina2009"),
        "role": "admin"
    },
    "TCDD2856": {
        "nom_complet": "Agent TCDD2856",
        "password": pwd_context.hash("Rare12@"),
        "role": "utilisateur"
    },
    "AGENT01": {
        "nom_complet": "Agent 01",
        "password": pwd_context.hash("Agent01@"),
        "role": "utilisateur"
    },
    "AGENT02": {
        "nom_complet": "Agent 02",
        "password": pwd_context.hash("Agent02@"),
        "role": "utilisateur"
    },
    "AGENT03": {
        "nom_complet": "Agent 03",
        "password": pwd_context.hash("Agent03@"),
        "role": "utilisateur"
    },
    "AGENT04": {
        "nom_complet": "Agent 04",
        "password": pwd_context.hash("Agent04@"),
        "role": "utilisateur"
    },
    "AGENT05": {
        "nom_complet": "Agent 05",
        "password": pwd_context.hash("Agent05@"),
        "role": "utilisateur"
    },
    "AGENT06": {
        "nom_complet": "Agent 06",
        "password": pwd_context.hash("Agent06@"),
        "role": "utilisateur"
    },
    "AGENT07": {
        "nom_complet": "Agent 07",
        "password": pwd_context.hash("Agent07@"),
        "role": "utilisateur"
    },
    "AGENT08": {
        "nom_complet": "Agent 08",
        "password": pwd_context.hash("Agent08@"),
        "role": "utilisateur"
    },
}


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def authenticate_user(login: str, password: str) -> dict | None:
    user = USERS.get(login.upper())
    if not user:
        return None
    if not verify_password(password, user["password"]):
        return None
    return {"login": login.upper(), "nom_complet": user["nom_complet"], "role": user["role"]}


def create_token(user_data: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "login": user_data["login"],
        "nom_complet": user_data["nom_complet"],
        "role": user_data["role"],
        "exp": expire
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
