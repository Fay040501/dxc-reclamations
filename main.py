"""
main.py — application FastAPI DXC Réclamations
Corrections apportées vs version originale :
  1. ACTIVE_SESSIONS dict mémoire → sessions PostgreSQL (session_set/get/delete)
  2. Startup event → init_sessions_table() au démarrage
  3. Rate limiting sur /login (5 tentatives / 60s par IP)
  4. Cookie sécurisé : samesite="lax" ajouté (secure=True à activer en prod HTTPS)
  5. /api/refresh-token corrigé : émettait session_token inexistant, ne renouvelait rien
  6. datetime.now() → datetime.now(timezone.utc) partout (non déprécié)
  7. import traceback, logging sorti des fonctions → en tête de fichier
  8. build_where : whitelist explicite des colonnes autorisées (protection injection)
  9. /api/redistribuer : validation agent_source != agent_cible côté backend
  10. /api/traiter : validation niveau obligatoire (1/2/3) côté backend
  11. /api/desassigner : vérification count == 0 → 404 si réclamation introuvable
  12. /api/reclamations : pagination LIMIT/OFFSET + total dans la réponse
  13. /api/counts : endpoint léger pour la sidebar (au lieu de /api/stats complet)
  14. Handler d'erreur global : RuntimeError DB ne remonte plus en 500 brut
  15. Logging structuré configuré au niveau app
  16. Form(...) retiré de login_submit — lecture via request.form() cohérente
"""
import csv
import io
import json
import logging
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import USERS, authenticate_user, create_token, decode_token
from database import (
    execute_db,
    execute_db_transaction,
    execute_many,  # noqa: F401 — disponible pour usage futur
    init_sessions_table,
    query_db,
    session_delete,
    session_get,
    session_set,
)

# ── Logging structuré ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dxc")

# ── Application ──
app = FastAPI(title="DXC Réclamations")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Handler d'erreur global ──
@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    logger.error(f"RuntimeError sur {request.url} : {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Erreur interne — contactez l'administrateur."},
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.error(
        f"Exception non gérée sur {request.url} : {exc}\n{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Erreur interne — contactez l'administrateur."},
    )


# ── Rate limiting simple (mémoire) sur /login ──
# Pour 20 utilisateurs sur réseau interne c'est largement suffisant.
_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_MAX = 5       # tentatives max
_RATE_WINDOW = 60   # fenêtre en secondes


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]
    _login_attempts[ip] = recent
    if len(recent) >= _RATE_MAX:
        return True
    _login_attempts[ip].append(now)
    return False


# ── Startup ──
@app.on_event("startup")
async def startup():
    init_sessions_table()
    logger.info("Application DXC Réclamations démarrée")


# ── Données métier ──
SOUS_MOTIFS: dict[str, list[str]] = {
    "CHURN": [
        "NOUVEL ABONNEMENT", "DÉCISION PERSONNELLE", "ZONE NON FIBRÉE",
        "PROBLEME TECHNIQUE", "LIGNE MULTIPLE", "VOYAGE", "UTILISATEUR INCONNU",
    ],
    "DEMANDE DE RESILIATION": [
        "NOUVEL ABONNEMENT", "DÉCISION PERSONNELLE", "ZONE NON FIBRÉE",
        "PROBLEME TECHNIQUE", "LIGNE MULTIPLE", "VOYAGE", "UTILISATEUR INCONNU",
    ],
    "INSTALLATION NON EFFECTUE": [
        "PB SATURE", "IMMEUBLE OU ZONE NON FIBRE", "IMPLANTATION DE POTEAUX",
        "CLIENT REMBOURSEE", "ANNULATION DE COMMANDE", "EN ATTENTE D'INSTALLATION",
    ],
    "PAS ENCORE INSTALLE": [
        "PB SATURE", "IMMEUBLE OU ZONE NON FIBRE", "IMPLANTATION DE POTEAUX",
        "CLIENT REMBOURSEE", "ANNULATION DE COMMANDE", "EN ATTENTE D'INSTALLATION",
    ],
    "TRANSFERT DE LIGNE": [
        "ABANDON DE LIGNE", "ZONE NON FIBRE", "DEMANDE INITIE", "INSTALLATION KO",
    ],
    "TRANSFERT NON EFFECTUE": [
        "ABANDON DE LIGNE", "ZONE NON FIBRE", "DEMANDE INITIE", "INSTALLATION KO",
    ],
}

MOTIFS_REELS: dict[str, list[str] | None] = {
    "CHURN": [
        "DECISION PERSONNELLE", "TRANSFERT HORS DELAI", "TRANSFERT NON EFFECTIF",
        "CHANGEMENT D'OFFRE", "PROBLEME FINANCIER", "SUBSTITUTION DE LIGNE",
        "RECHARGEMENT IMPOSSIBLE", "AUTRES",
    ],
    "DEMANDE DE RESILIATION": [
        "DECISION PERSONNELLE", "TRANSFERT HORS DELAI", "TRANSFERT NON EFFECTIF",
        "CHANGEMENT D'OFFRE", "PROBLEME FINANCIER", "SUBSTITUTION DE LIGNE",
        "RECHARGEMENT IMPOSSIBLE", "AUTRES",
    ],
    "INSTALLATION NON EFFECTUE": None,  # champ texte libre
    "PAS ENCORE INSTALLE": None,        # champ texte libre
    "TRANSFERT DE LIGNE": [
        "ZONE NON FIBREE", "IMMEUBLE NON FIBRE", "DEMANDE INITIEE",
        "INSTALLATION EN COURS", "INSTALLATION OK", "DEMENAGEMENT NOK",
        "DEMENAGEMENT EN COURS", "HORS PERIMETRE", "NOUVEAU RDV",
        "DEMANDE A INITIER", "TRANSFERT ANNULE", "ZONE NON ELECTRIFIE",
        "CLIENTS INDECIS", "AUTRES",
    ],
    "TRANSFERT NON EFFECTUE": [
        "ZONE NON FIBREE", "IMMEUBLE NON FIBRE", "DEMANDE INITIEE",
        "INSTALLATION EN COURS", "INSTALLATION OK", "DEMENAGEMENT NOK",
        "DEMENAGEMENT EN COURS", "HORS PERIMETRE", "NOUVEAU RDV",
        "DEMANDE A INITIER", "TRANSFERT ANNULE", "ZONE NON ELECTRIFIE",
        "CLIENTS INDECIS", "AUTRES",
    ],
}

# Whitelist colonnes autorisées dans build_where (prévient toute injection via nom de colonne)
_ALLOWED_COLS = frozenset({
    "statut_traitement",
    "motif_non_paiement",
    "categorie_de_non_paiement",
    "campagne",
    "assigne_a",
})

# Cache filtres (TTL 10 min)
FILTERS_CACHE: dict = {"data": None, "loaded_at": None}


def load_filters_cache() -> dict:
    annees = [
        r["a"] for r in query_db(
            "SELECT DISTINCT EXTRACT(YEAR FROM startdate)::text AS a "
            "FROM tb_reclamations WHERE startdate IS NOT NULL ORDER BY a DESC"
        )
    ]
    mois_set = [
        r["m"] for r in query_db(
            "SELECT DISTINCT LPAD(EXTRACT(MONTH FROM startdate)::text, 2, '0') AS m "
            "FROM tb_reclamations WHERE startdate IS NOT NULL ORDER BY m"
        )
    ]
    campagnes = [
        r["campagne"] for r in query_db(
            "SELECT DISTINCT campagne FROM tb_reclamations "
            "WHERE campagne IS NOT NULL ORDER BY campagne"
        )
    ]
    motifs = [
        r["motif_non_paiement"] for r in query_db(
            "SELECT DISTINCT motif_non_paiement FROM tb_reclamations "
            "WHERE motif_non_paiement IS NOT NULL ORDER BY motif_non_paiement"
        )
    ]
    categories = [
        r["categorie_de_non_paiement"] for r in query_db(
            "SELECT DISTINCT categorie_de_non_paiement FROM tb_reclamations "
            "WHERE categorie_de_non_paiement IS NOT NULL ORDER BY categorie_de_non_paiement"
        )
    ]
    FILTERS_CACHE["data"] = {
        "annees": annees, "mois": mois_set,
        "campagnes": campagnes, "motifs": motifs, "categories": categories,
    }
    FILTERS_CACHE["loaded_at"] = datetime.now(timezone.utc)
    return FILTERS_CACHE["data"]


def get_filters_cached() -> dict:
    if FILTERS_CACHE["data"] is None or (
        datetime.now(timezone.utc) - FILTERS_CACHE["loaded_at"]
    ).total_seconds() > 600:
        return load_filters_cache()
    return FILTERS_CACHE["data"]


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("token")
    if not token:
        return None
    user = decode_token(token)
    if not user:
        return None
    # Vérification en base — invalide les sessions concurrentes (session unique par login)
    try:
        active_token = session_get(user["login"])
    except Exception:
        return None
    if active_token != token:
        return None
    return user


def get_utilisateurs() -> dict:
    return {k: v for k, v in USERS.items() if v["role"] == "utilisateur"}


def build_where(
    annee=None, mois=None, jour=None, extra: dict | None = None
) -> tuple[str, list]:
    """
    Construit la clause WHERE paramétrée.
    Les noms de colonnes dans `extra` sont vérifiés contre une whitelist
    pour éviter toute injection SQL via le nom de colonne.
    """
    clauses: list[str] = []
    params: list = []
    if annee:
        clauses.append("EXTRACT(YEAR FROM startdate) = %s")
        params.append(int(annee))
    if mois:
        clauses.append("EXTRACT(MONTH FROM startdate) = %s")
        params.append(int(mois))
    if jour:
        clauses.append("EXTRACT(DAY FROM startdate) = %s")
        params.append(int(jour))
    if extra:
        for col, val in extra.items():
            if col not in _ALLOWED_COLS:
                logger.warning(f"build_where : colonne non autorisée ignorée → '{col}'")
                continue
            if val is not None and val != "":
                clauses.append(f"{col} = %s")
                params.append(val)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _serialize_row(row: dict, date_fields: set[str] | None = None) -> dict:
    """Convertit les objets date/datetime d'un dict en strings JSON-sérialisables."""
    if date_fields is None:
        date_fields = {"date_assignation", "date_traitement"}
    for k, v in row.items():
        if isinstance(v, datetime):
            row[k] = v.strftime("%Y-%m-%d") if k in date_fields else v.isoformat()
        elif hasattr(v, "isoformat"):  # date, Decimal, etc.
            row[k] = str(v)
    return row


# ============================================================
# AUTH
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/app", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request):
    # Rate limiting sur l'IP avant tout traitement
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        logger.warning(f"Rate limit /login — ip={client_ip}")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Trop de tentatives. Patientez 1 minute avant de réessayer.",
            },
        )

    form = await request.form()
    login_val = str(form.get("login", "")).strip()
    password_val = str(form.get("password", ""))

    user = authenticate_user(login_val, password_val)
    if not user:
        logger.info(f"Échec connexion — login={login_val.upper()} ip={client_ip}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Login ou mot de passe incorrect"},
        )

    token = create_token(user)
    # Persiste en base — invalide automatiquement toute session précédente pour ce login
    session_set(user["login"], token)
    logger.info(f"Connexion réussie — login={user['login']} ip={client_ip}")

    resp = RedirectResponse(url="/app", status_code=303)
    resp.set_cookie(
        key="token",
        value=token,
        httponly=True,      # inaccessible via JS — protège contre XSS
        samesite="lax",     # protège contre CSRF
        max_age=28800,      # 8h
        # secure=True       # À décommenter en production (HTTPS obligatoire)
    )
    return resp


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.post("/api/refresh-token")
async def refresh_token(request: Request, response: Response):
    """
    Renouvelle le cookie JWT si la session est encore valide.
    Corrigé : l'original lisait 'session_token' (inexistant) et ne renouvelait rien.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée")
    new_token = create_token(user)
    session_set(user["login"], new_token)  # Remplace en base
    response.set_cookie(
        key="token",
        value=new_token,
        httponly=True,
        samesite="lax",
        max_age=28800,
        # secure=True
    )
    logger.info(f"Token rafraîchi — login={user['login']}")
    return {"refreshed": True}


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("token")
    if token:
        user = decode_token(token)
        if user:
            try:
                session_delete(user["login"])
                logger.info(f"Déconnexion — login={user['login']}")
            except Exception:
                pass  # On déconnecte quand même côté cookie
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("token")
    return resp


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    filters_data = get_filters_cached()
    last_annee = (
        filters_data["annees"][0]
        if filters_data["annees"]
        else str(datetime.now(timezone.utc).year)
    )
    last_mois = (
        filters_data["mois"][-1]
        if filters_data["mois"]
        else str(datetime.now(timezone.utc).month).zfill(2)
    )
    return templates.TemplateResponse(
        "app.html",
        {
            "request": request,
            "user_json": json.dumps(user),
            "utilisateurs_json": json.dumps(
                {k: {"nom_complet": v["nom_complet"]} for k, v in get_utilisateurs().items()}
            ),
            "sous_motifs_json": json.dumps(SOUS_MOTIFS),
            "motifs_reels_json": json.dumps(MOTIFS_REELS, ensure_ascii=False),
            "filters_json": json.dumps(filters_data),
            "default_annee": last_annee,
            "default_mois": last_mois,
        },
    )


# ============================================================
# API — FILTRES
# ============================================================

@app.get("/api/filters")
async def api_filters(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return get_filters_cached()


@app.get("/api/filters/refresh")
async def api_filters_refresh(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return load_filters_cache()


# ============================================================
# API — COUNTS (sidebar — endpoint léger)
# ============================================================

@app.get("/api/counts")
async def api_counts(request: Request):
    """
    Retourne uniquement les compteurs pour la sidebar.
    Remplace l'appel à /api/stats complet à chaque changement de page.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    if user["role"] == "admin":
        row = query_db("""
            SELECT
                SUM(CASE WHEN statut_traitement = 'NON ASSIGNE' THEN 1 ELSE 0 END) AS non_assignees,
                SUM(CASE WHEN statut_traitement = 'TRAITE'      THEN 1 ELSE 0 END) AS traitees
            FROM tb_reclamations
        """)[0]
        return {
            "non_assignees": row["non_assignees"] or 0,
            "traitees": row["traitees"] or 0,
        }
    else:
        row = query_db("""
            SELECT COUNT(*) AS assignees
            FROM tb_reclamations
            WHERE assigne_a = %s AND statut_traitement = 'ASSIGNE'
        """, [user["login"]])[0]
        return {"assignees": row["assignees"] or 0}


# ============================================================
# API — RÉCLAMATIONS (avec pagination)
# ============================================================

@app.get("/api/reclamations")
async def api_reclamations(
    request: Request,
    annee: Optional[str] = None,
    mois: Optional[str] = None,
    jour: Optional[str] = None,
    campagne: Optional[str] = None,
    statut: Optional[str] = None,
    motif: Optional[str] = None,
    categorie: Optional[str] = None,
    assigne_a: Optional[str] = None,
    date_assign_du: Optional[str] = None,
    date_assign_au: Optional[str] = None,
    date_trait_du: Optional[str] = None,
    date_trait_au: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    page: int = 1,
    per_page: int = 100,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    # Support page/per_page (frontend) ou limit/offset (API directe)
    if page > 1 or per_page != 100:
        per_page = max(1, min(per_page, 500))
        page = max(1, page)
        limit = per_page
        offset = (page - 1) * per_page
    else:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)

    extra: dict = {}
    if statut:
        extra["statut_traitement"] = statut
    if motif:
        extra["motif_non_paiement"] = motif
    if categorie:
        extra["categorie_de_non_paiement"] = categorie
    if campagne:
        extra["campagne"] = campagne
    # L'utilisateur est TOUJOURS limité à ses propres réclamations — non contournable
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]
    elif assigne_a:
        extra["assigne_a"] = assigne_a

    where, params = build_where(annee, mois, jour, extra)

    if date_assign_du:
        where += " AND date_assignation >= %s"
        params.append(date_assign_du)
    if date_assign_au:
        where += " AND date_assignation <= %s"
        params.append(date_assign_au + " 23:59:59")
    if date_trait_du:
        where += " AND date_traitement >= %s"
        params.append(date_trait_du)
    if date_trait_au:
        where += " AND date_traitement <= %s"
        params.append(date_trait_au + " 23:59:59")

    # Agrégat total + breakdown statuts en une seule requête
    agg = query_db(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN statut_traitement = 'NON ASSIGNE' THEN 1 ELSE 0 END) AS non_assignees,
            SUM(CASE WHEN statut_traitement = 'ASSIGNE'     THEN 1 ELSE 0 END) AS assignees,
            SUM(CASE WHEN statut_traitement = 'TRAITE'      THEN 1 ELSE 0 END) AS traitees
        FROM tb_reclamations WHERE {where}
    """, params)[0]
    total = agg["total"] or 0

    sql = f"""
        SELECT id_hash, startdate, nd_clean, identite_client, contact,
               disponibilite_client, categorie_de_non_paiement, motif_non_paiement,
               commentaire, campagne, statut_appel, motif_reel, niveau,
               commentaire_bo, zone_client, sous_motif, id_dossier,
               assigne_a, statut_traitement, date_assignation, date_traitement
        FROM tb_reclamations
        WHERE {where}
        ORDER BY startdate DESC
        LIMIT %s OFFSET %s
    """
    data = [_serialize_row(r) for r in query_db(sql, params + [limit, offset])]

    return {
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
        "counts": {
            "non_assignees": agg["non_assignees"] or 0,
            "assignees":     agg["assignees"] or 0,
            "traitees":      agg["traitees"] or 0,
        },
    }


# ============================================================
# API — DISPATCH COUNTS (catégories + motifs NON ASSIGNÉS — toutes pages)
# ============================================================

@app.get("/api/dispatch-counts")
async def api_dispatch_counts(
    request: Request,
    annee: Optional[str] = None,
    mois: Optional[str] = None,
    jour: Optional[str] = None,
    campagne: Optional[str] = None,
):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)

    extra: dict = {"statut_traitement": "NON ASSIGNE"}
    if campagne:
        extra["campagne"] = campagne

    where, params = build_where(annee, mois, jour, extra)

    cat_rows = query_db(f"""
        SELECT COALESCE(categorie_de_non_paiement, '__NULL__') AS val, COUNT(*) AS cnt
        FROM tb_reclamations WHERE {where}
        GROUP BY val ORDER BY cnt DESC
    """, params)
    mot_rows = query_db(f"""
        SELECT COALESCE(motif_non_paiement, '__NULL__') AS val, COUNT(*) AS cnt
        FROM tb_reclamations WHERE {where}
        GROUP BY val ORDER BY cnt DESC
    """, params)

    return {
        "categories": [[r["val"], r["cnt"]] for r in cat_rows],
        "motifs":     [[r["val"], r["cnt"]] for r in mot_rows],
    }


# ============================================================
# API — STATS
# ============================================================

@app.get("/api/stats")
async def api_stats(
    request: Request,
    annee: Optional[str] = None,
    mois: Optional[str] = None,
    jour: Optional[str] = None,
    campagne: Optional[str] = None,
    statut: Optional[str] = None,
    motif: Optional[str] = None,
    categorie: Optional[str] = None,
    date_assign_du: Optional[str] = None,
    date_assign_au: Optional[str] = None,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    extra: dict = {}
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]
    if statut:
        extra["statut_traitement"] = statut
    if motif:
        extra["motif_non_paiement"] = motif
    if categorie:
        extra["categorie_de_non_paiement"] = categorie
    if campagne:
        extra["campagne"] = campagne

    where, params = build_where(annee, mois, jour, extra)

    # Filtre date assignation — appliqué uniquement pour volume_par_agent
    agent_where = where
    agent_params = list(params)
    if date_assign_du:
        agent_where += " AND date_assignation >= %s"
        agent_params.append(date_assign_du)
    if date_assign_au:
        agent_where += " AND date_assignation <= %s"
        agent_params.append(date_assign_au + " 23:59:59")

    counts = query_db(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN statut_traitement = 'NON ASSIGNE' THEN 1 ELSE 0 END) AS non_assignees,
            SUM(CASE WHEN statut_traitement = 'ASSIGNE'     THEN 1 ELSE 0 END) AS assignees,
            SUM(CASE WHEN statut_traitement = 'TRAITE'      THEN 1 ELSE 0 END) AS traitees,
            SUM(CASE WHEN niveau = '1' THEN 1 ELSE 0 END) AS n1,
            SUM(CASE WHEN niveau = '2' THEN 1 ELSE 0 END) AS n2
        FROM tb_reclamations WHERE {where}
    """, params)[0]
    total = counts["total"] or 0
    traitees = counts["traitees"] or 0
    taux = round(traitees / total * 100, 1) if total > 0 else 0

    non_decroche = query_db(f"""
        SELECT COUNT(*) AS cnt FROM tb_reclamations
        WHERE {where}
          AND (LOWER(disponibilite_client) LIKE '%%joignable%%'
               OR LOWER(disponibilite_client) LIKE '%%sonne en vain%%')
    """, params)[0]["cnt"] or 0
    pct_non_decroche = round(non_decroche / total * 100, 1) if total > 0 else 0

    delai_row = query_db(f"""
        SELECT AVG(EXTRACT(EPOCH FROM (date_traitement - date_assignation)) / 3600) AS delai
        FROM tb_reclamations
        WHERE {where} AND date_assignation IS NOT NULL AND date_traitement IS NOT NULL
    """, params)[0]
    delai_moyen = round(delai_row["delai"], 1) if delai_row["delai"] else 0

    motif_rows = query_db(f"""
        SELECT
            COALESCE(motif_non_paiement, categorie_de_non_paiement, 'Non renseigné') AS motif,
            statut_traitement,
            COUNT(*) AS cnt
        FROM tb_reclamations WHERE {where}
        GROUP BY motif, statut_traitement
        ORDER BY cnt DESC
    """, params)
    par_motif_traite: dict[str, int] = {}
    par_motif_non_traite: dict[str, int] = {}
    for r in motif_rows:
        m = r["motif"]
        if r["statut_traitement"] == "TRAITE":
            par_motif_traite[m] = r["cnt"]
        else:
            par_motif_non_traite[m] = par_motif_non_traite.get(m, 0) + r["cnt"]
    all_motifs = sorted(
        set(list(par_motif_traite) + list(par_motif_non_traite)),
        key=lambda m: par_motif_traite.get(m, 0) + par_motif_non_traite.get(m, 0),
        reverse=True,
    )[:15]

    par_jour = {
        str(r["jour"]): r["cnt"]
        for r in query_db(f"""
            SELECT startdate::date AS jour, COUNT(*) AS cnt
            FROM tb_reclamations WHERE {where} AND startdate IS NOT NULL
            GROUP BY jour ORDER BY jour
        """, params)
    }

    volume_par_agent: dict[str, dict] = {}
    for r in query_db(f"""
        SELECT assigne_a,
            COUNT(*) AS total,
            SUM(CASE WHEN statut_traitement = 'TRAITE' THEN 1 ELSE 0 END) AS traite
        FROM tb_reclamations WHERE {agent_where} AND assigne_a IS NOT NULL
        GROUP BY assigne_a
    """, agent_params):
        volume_par_agent[r["assigne_a"]] = {
            "total": r["total"],
            "traite": r["traite"],
            "restant": r["total"] - r["traite"],
        }

    return {
        "total": total,
        "non_assignees": counts["non_assignees"] or 0,
        "assignees": counts["assignees"] or 0,
        "traitees": traitees,
        "taux": taux,
        "non_decroche": non_decroche,
        "pct_non_decroche": pct_non_decroche,
        "n1": counts["n1"] or 0,
        "n2": counts["n2"] or 0,
        "delai_moyen": delai_moyen,
        "motif_labels": all_motifs,
        "motif_traite_vals": [par_motif_traite.get(m, 0) for m in all_motifs],
        "motif_non_traite_vals": [par_motif_non_traite.get(m, 0) for m in all_motifs],
        "par_jour": par_jour,
        "volume_par_agent": volume_par_agent,
    }


# ============================================================
# API — ACTIONS
# ============================================================

@app.post("/api/assigner-nombre")
async def api_assigner_nombre(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)

    body = await request.json()
    motifs: list = body.get("motifs", [])
    categories: list = body.get("categories", [])
    campagnes_f: list = body.get("campagnes", [])
    nombre: int = int(body.get("nombre", 0))
    assigne_a: str = body.get("assigne_a", "")
    annee: str = body.get("annee", "")
    mois: str = body.get("mois", "")
    jour: str = body.get("jour", "")

    if not assigne_a:
        raise HTTPException(status_code=422, detail="Agent non spécifié.")
    if nombre <= 0:
        raise HTTPException(status_code=422, detail="Le nombre doit être supérieur à 0.")

    clauses = ["statut_traitement = 'NON ASSIGNE'"]
    params: list = []

    if annee:
        clauses.append("EXTRACT(YEAR FROM startdate) = %s")
        params.append(int(annee))
    if mois:
        clauses.append("EXTRACT(MONTH FROM startdate) = %s")
        params.append(int(mois))
    if jour:
        clauses.append("EXTRACT(DAY FROM startdate) = %s")
        params.append(int(jour))

    if motifs:
        real_motifs = [m for m in motifs if m != "__NULL__"]
        has_null = "__NULL__" in motifs
        sub: list[str] = []
        if real_motifs:
            ph = ",".join(["%s"] * len(real_motifs))
            sub.append(f"motif_non_paiement IN ({ph})")
            params.extend(real_motifs)
        if has_null:
            sub.append("motif_non_paiement IS NULL")
        if sub:
            clauses.append("(" + " OR ".join(sub) + ")")

    if categories:
        real_cats = [c for c in categories if c != "__NULL__"]
        has_null = "__NULL__" in categories
        sub = []
        if real_cats:
            ph = ",".join(["%s"] * len(real_cats))
            sub.append(f"categorie_de_non_paiement IN ({ph})")
            params.extend(real_cats)
        if has_null:
            sub.append("categorie_de_non_paiement IS NULL")
        if sub:
            clauses.append("(" + " OR ".join(sub) + ")")

    if campagnes_f:
        ph = ",".join(["%s"] * len(campagnes_f))
        clauses.append(f"campagne IN ({ph})")
        params.extend(campagnes_f)

    where = " AND ".join(clauses)
    params.append(nombre)

    sql_select = (
        f"SELECT id_hash FROM tb_reclamations WHERE {where} ORDER BY startdate LIMIT %s"
    )
    sql_update_prefix = """
        UPDATE tb_reclamations
        SET assigne_a = %s, statut_traitement = 'ASSIGNE', date_assignation = %s
        WHERE id_hash IN
    """
    try:
        rows = execute_db_transaction(
            sql_select, params,
            sql_update_prefix, [assigne_a, datetime.now(timezone.utc)],
        )
        logger.info(
            f"Dispatch — {len(rows)} réclamations → {assigne_a} par {user['login']}"
        )
        return {"assigned": len(rows)}
    except Exception as e:
        logger.error(f"DISPATCH ERROR: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/desassigner")
async def api_desassigner(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)
    body = await request.json()
    id_hash = body.get("id_hash")
    if not id_hash:
        raise HTTPException(status_code=422, detail="id_hash manquant.")
    count = execute_db(
        """UPDATE tb_reclamations
           SET assigne_a = NULL, statut_traitement = 'NON ASSIGNE', date_assignation = NULL
           WHERE id_hash = %s""",
        [id_hash],
    )
    if count == 0:
        raise HTTPException(status_code=404, detail="Réclamation introuvable.")
    logger.info(f"Désassignation — id_hash={id_hash} par {user['login']}")
    return {"success": True}


@app.get("/api/stock-agent")
async def api_stock_agent(request: Request, agent: str):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)
    return query_db("""
        SELECT
            COALESCE(motif_non_paiement, categorie_de_non_paiement, 'Non défini') AS motif,
            COUNT(*) AS volume
        FROM tb_reclamations
        WHERE assigne_a = %s AND statut_traitement = 'ASSIGNE'
        GROUP BY motif ORDER BY volume DESC
    """, [agent])


@app.post("/api/redistribuer")
async def api_redistribuer(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)

    body = await request.json()
    agent_source: str = body.get("agent_source", "")
    agent_cible: str = body.get("agent_cible", "")
    nombre: int = int(body.get("nombre", 0))

    if not agent_source or not agent_cible:
        raise HTTPException(status_code=422, detail="Agent source et agent cible requis.")
    # Validation source ≠ cible — manquait dans l'original
    if agent_source == agent_cible:
        raise HTTPException(
            status_code=422,
            detail="Agent source et agent cible doivent être différents.",
        )
    if nombre <= 0:
        raise HTTPException(status_code=422, detail="Le nombre doit être supérieur à 0.")

    if body.get("motif"):
        rows = query_db("""
            SELECT id_hash FROM tb_reclamations
            WHERE assigne_a = %s AND statut_traitement = 'ASSIGNE'
              AND COALESCE(motif_non_paiement, categorie_de_non_paiement) = %s
            ORDER BY startdate LIMIT %s
        """, [agent_source, body["motif"], nombre])
    else:
        rows = query_db("""
            SELECT id_hash FROM tb_reclamations
            WHERE assigne_a = %s AND statut_traitement = 'ASSIGNE'
            ORDER BY startdate LIMIT %s
        """, [agent_source, nombre])

    if rows:
        ids = [r["id_hash"] for r in rows]
        ph = ",".join(["%s"] * len(ids))
        execute_db(
            f"UPDATE tb_reclamations SET assigne_a = %s, date_assignation = %s "
            f"WHERE id_hash IN ({ph})",
            [agent_cible, datetime.now(timezone.utc)] + ids,
        )
    logger.info(
        f"Redistribution — {len(rows)} réclamations de {agent_source} "
        f"→ {agent_cible} par {user['login']}"
    )
    return {"redistributed": len(rows)}


@app.post("/api/traiter")
async def api_traiter(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()

    # Validation statut_appel — obligatoire
    if not body.get("statut_appel"):
        raise HTTPException(status_code=422, detail="Le statut de l'appel est obligatoire.")

    # Validation niveau — obligatoire et valeur contrôlée
    niveau = str(body.get("niveau", "")).strip()
    if niveau not in {"1", "2", "3"}:
        raise HTTPException(status_code=422, detail="Le niveau est obligatoire (1, 2 ou 3).")

    where_owner = "id_hash = %s"
    owner_params = [body["id_hash"]]
    if user["role"] == "utilisateur":
        # L'agent ne peut traiter que ses propres réclamations
        where_owner += " AND assigne_a = %s"
        owner_params.append(user["login"])

    count = execute_db(
        f"""UPDATE tb_reclamations SET
                statut_appel    = %s,
                motif_reel      = %s,
                niveau          = %s,
                commentaire_bo  = %s,
                zone_client     = %s,
                sous_motif      = %s,
                id_dossier      = %s,
                statut_traitement = 'TRAITE',
                date_traitement = %s
            WHERE {where_owner}""",
        [
            body.get("statut_appel"),
            body.get("motif_reel", ""),
            niveau,
            body.get("commentaire_bo", ""),
            body.get("zone_client", ""),
            body.get("sous_motif", ""),
            body.get("id_dossier", ""),
            datetime.now(timezone.utc),
        ] + owner_params,
    )
    if count == 0:
        raise HTTPException(status_code=403, detail="Action non autorisée sur cette réclamation.")
    logger.info(f"Traitement — id_hash={body['id_hash']} par {user['login']}")
    return {"success": True}


@app.post("/api/modifier-commentaire")
async def api_modifier_commentaire(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()

    where_owner = "id_hash = %s"
    owner_params = [body["id_hash"]]
    if user["role"] == "utilisateur":
        where_owner += " AND assigne_a = %s"
        owner_params.append(user["login"])

    count = execute_db(
        f"UPDATE tb_reclamations SET commentaire_bo = %s WHERE {where_owner}",
        [body.get("commentaire_bo", "")] + owner_params,
    )
    if count == 0:
        raise HTTPException(status_code=403, detail="Action non autorisée sur cette réclamation.")
    return {"success": True}


# ============================================================
# EXPORT CSV
# ============================================================

@app.get("/api/export/csv")
async def export_csv(
    request: Request,
    annee: Optional[str] = None,
    mois: Optional[str] = None,
    jour: Optional[str] = None,
    statut: Optional[str] = None,
    motif: Optional[str] = None,
    categorie: Optional[str] = None,
    campagne: Optional[str] = None,
    date_assign_du: Optional[str] = None,
    date_assign_au: Optional[str] = None,
    date_trait_du: Optional[str] = None,
    date_trait_au: Optional[str] = None,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    extra: dict = {}
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]
    if statut:
        extra["statut_traitement"] = statut
    if motif:
        extra["motif_non_paiement"] = motif
    if categorie:
        extra["categorie_de_non_paiement"] = categorie
    if campagne:
        extra["campagne"] = campagne

    where, params = build_where(annee, mois, jour, extra)
    if date_assign_du:
        where += " AND date_assignation >= %s"
        params.append(date_assign_du)
    if date_assign_au:
        where += " AND date_assignation <= %s"
        params.append(date_assign_au + " 23:59:59")
    if date_trait_du:
        where += " AND date_traitement >= %s"
        params.append(date_trait_du)
    if date_trait_au:
        where += " AND date_traitement <= %s"
        params.append(date_trait_au + " 23:59:59")

    data = [
        _serialize_row(r)
        for r in query_db(
            f"SELECT * FROM tb_reclamations WHERE {where} ORDER BY startdate DESC",
            params,
        )
    ]
    if not data:
        return JSONResponse({"error": "Aucune donnée pour les filtres sélectionnés."})

    output = io.StringIO("\ufeff")  # BOM UTF-8 — Excel Windows l'attend
    writer = csv.DictWriter(output, fieldnames=data[0].keys(), delimiter=";")
    writer.writeheader()
    writer.writerows(data)

    filename = (
        f"reclamations_{user['login']}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv"
    )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
