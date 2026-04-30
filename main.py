from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import query_db, execute_db, execute_many, execute_db_transaction
from auth import authenticate_user, create_token, decode_token, USERS
from datetime import datetime
from typing import Optional
import csv
import io
import json

app = FastAPI(title="DXC Réclamations")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SOUS_MOTIFS = {
    "TRANSFERT DE LIGNE / DEMENAGEMENT": ["ABANDON DE LIGNE", "NOUVEL ABONNEMENT", "CHURN", "DEMANDE INITIE", "ZONE NON FIBREE"],
    "TRANSFERT DE LIGNE / DÉMÉNAGEMENT": ["ABANDON DE LIGNE", "NOUVEL ABONNEMENT", "CHURN", "DEMANDE INITIE", "ZONE NON FIBREE"],
    "CHURN": ["ABANDON DE LIGNE", "OFFRE CONCURRENTE", "PROBLEME TECHNIQUE"],
    "DEMANDE DE RESILIATION": ["ABANDON DE LIGNE", "OFFRE CONCURRENTE", "PROBLEME TECHNIQUE"],
    "DEMANDE RÉSILIATION": ["ABANDON DE LIGNE", "OFFRE CONCURRENTE", "PROBLEME TECHNIQUE"],
}

# Cache filtres
FILTERS_CACHE = {"data": None, "loaded_at": None}

def load_filters_cache():
    annees = [r["a"] for r in query_db("SELECT DISTINCT EXTRACT(YEAR FROM startdate)::text AS a FROM tb_reclamations WHERE startdate IS NOT NULL ORDER BY a DESC")]
    mois_set = [r["m"] for r in query_db("SELECT DISTINCT LPAD(EXTRACT(MONTH FROM startdate)::text, 2, '0') AS m FROM tb_reclamations WHERE startdate IS NOT NULL ORDER BY m")]
    campagnes = [r["campagne"] for r in query_db("SELECT DISTINCT campagne FROM tb_reclamations WHERE campagne IS NOT NULL ORDER BY campagne")]
    motifs = [r["motif_non_paiement"] for r in query_db("SELECT DISTINCT motif_non_paiement FROM tb_reclamations WHERE motif_non_paiement IS NOT NULL ORDER BY motif_non_paiement")]
    categories = [r["categorie_de_non_paiement"] for r in query_db("SELECT DISTINCT categorie_de_non_paiement FROM tb_reclamations WHERE categorie_de_non_paiement IS NOT NULL ORDER BY categorie_de_non_paiement")]
    FILTERS_CACHE["data"] = {"annees": annees, "mois": mois_set, "campagnes": campagnes, "motifs": motifs, "categories": categories}
    FILTERS_CACHE["loaded_at"] = datetime.now()
    return FILTERS_CACHE["data"]

def get_filters_cached():
    if FILTERS_CACHE["data"] is None or (datetime.now() - FILTERS_CACHE["loaded_at"]).total_seconds() > 600:
        return load_filters_cache()
    return FILTERS_CACHE["data"]


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("token")
    if not token:
        return None
    return decode_token(token)

def get_utilisateurs():
    return {k: v for k, v in USERS.items() if v["role"] == "utilisateur"}


def build_where(annee=None, mois=None, jour=None, extra=None):
    """Construit la clause WHERE et les paramètres."""
    clauses = []
    params = []
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
            if val:
                clauses.append(f"{col} = %s")
                params.append(val)
    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


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
async def login_submit(request: Request, login: str = Form(...), password: str = Form(...)):
    user = authenticate_user(login, password)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Login ou mot de passe incorrect"})
    token = create_token(user)
    response = RedirectResponse(url="/app", status_code=303)
    response.set_cookie(key="token", value=token, httponly=True, max_age=28800)
    return response

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.post("/api/refresh-token")
async def refresh_token(request: Request, response: Response):
    """Renouvelle le cookie de session si l'utilisateur est encore authentifié."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    # Renouveler le cookie pour 8h supplémentaires
    response.set_cookie(
        key="session_token",
        value=request.cookies.get("session_token"),
        max_age=28800,
        httponly=True,
        samesite="lax"
    )
    return {"refreshed": True}

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("token")
    return response

@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    filters_data = get_filters_cached()
    last_annee = filters_data["annees"][0] if filters_data["annees"] else str(datetime.now().year)
    last_mois = filters_data["mois"][-1] if filters_data["mois"] else str(datetime.now().month).zfill(2)
    return templates.TemplateResponse("app.html", {
        "request": request,
        "user_json": json.dumps(user),
        "utilisateurs_json": json.dumps({k: {"nom_complet": v["nom_complet"]} for k, v in get_utilisateurs().items()}),
        "sous_motifs_json": json.dumps(SOUS_MOTIFS),
        "filters_json": json.dumps(filters_data),
        "default_annee": last_annee,
        "default_mois": last_mois
    })


# ============================================================
# API - FILTRES
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
# API - RECLAMATIONS
# ============================================================
@app.get("/api/reclamations")
async def api_reclamations(
    request: Request,
    annee: Optional[str] = None, mois: Optional[str] = None, jour: Optional[str] = None,
    campagne: Optional[str] = None, statut: Optional[str] = None,
    motif: Optional[str] = None, categorie: Optional[str] = None,
    assigne_a: Optional[str] = None,
    date_assign_du: Optional[str] = None, date_assign_au: Optional[str] = None,
    date_trait_du: Optional[str] = None, date_trait_au: Optional[str] = None,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    extra = {}
    if statut: extra["statut_traitement"] = statut
    if motif: extra["motif_non_paiement"] = motif
    if categorie: extra["categorie_de_non_paiement"] = categorie
    if campagne: extra["campagne"] = campagne
    # L'admin peut filtrer par agent, l'utilisateur est toujours limité à lui-même
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]  # Forçage en dernier — non contournable
    elif assigne_a:
        extra["assigne_a"] = assigne_a

    where, params = build_where(annee, mois, jour, extra)
    # Filtres date_assignation
    if date_assign_du:
        where += " AND date_assignation >= %s"; params.append(date_assign_du)
    if date_assign_au:
        where += " AND date_assignation <= %s"; params.append(date_assign_au + " 23:59:59")
    # Filtres date_traitement
    if date_trait_du:
        where += " AND date_traitement >= %s"; params.append(date_trait_du)
    if date_trait_au:
        where += " AND date_traitement <= %s"; params.append(date_trait_au + " 23:59:59")
    sql = f"""
        SELECT id_hash, startdate, nd_clean, identite_client, contact,
               disponibilite_client, categorie_de_non_paiement, motif_non_paiement,
               commentaire, campagne, statut_appel, motif_reel, niveau,
               commentaire_bo, zone_client, sous_motif, id_dossier,
               assigne_a, statut_traitement, date_assignation, date_traitement
        FROM tb_reclamations
        WHERE {where}
        ORDER BY startdate DESC
    """
    data = query_db(sql, params)
    # Convertir les datetimes en strings pour JSON — format YYYY-MM-DD
    DATE_FIELDS = {"date_assignation", "date_traitement"}
    for row in data:
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.strftime('%Y-%m-%d') if k in DATE_FIELDS else v.isoformat()
    return data


# ============================================================
# API - STATS
# ============================================================
@app.get("/api/stats")
async def api_stats(
    request: Request,
    annee: Optional[str] = None, mois: Optional[str] = None, jour: Optional[str] = None,
    campagne: Optional[str] = None, statut: Optional[str] = None,
    motif: Optional[str] = None, categorie: Optional[str] = None,
    date_assign_du: Optional[str] = None, date_assign_au: Optional[str] = None,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    extra = {}
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]
    if statut: extra["statut_traitement"] = statut
    if motif: extra["motif_non_paiement"] = motif
    if categorie: extra["categorie_de_non_paiement"] = categorie
    if campagne: extra["campagne"] = campagne

    where, params = build_where(annee, mois, jour, extra)

    # Filtre date assignation — uniquement pour la requête volume_par_agent
    agent_where = where
    agent_params = list(params)
    if date_assign_du:
        agent_where += " AND date_assignation >= %s"; agent_params.append(date_assign_du)
    if date_assign_au:
        agent_where += " AND date_assignation <= %s"; agent_params.append(date_assign_au + " 23:59:59")

    # Stats agrégées côté SQL — ultra rapide
    sql_counts = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN statut_traitement = 'NON ASSIGNE' THEN 1 ELSE 0 END) AS non_assignees,
            SUM(CASE WHEN statut_traitement = 'ASSIGNE' THEN 1 ELSE 0 END) AS assignees,
            SUM(CASE WHEN statut_traitement = 'TRAITE' THEN 1 ELSE 0 END) AS traitees,
            SUM(CASE WHEN niveau = '1' THEN 1 ELSE 0 END) AS n1,
            SUM(CASE WHEN niveau = '2' THEN 1 ELSE 0 END) AS n2
        FROM tb_reclamations WHERE {where}
    """
    counts = query_db(sql_counts, params)[0]
    total = counts["total"] or 0
    traitees = counts["traitees"] or 0
    taux = round((traitees / total * 100), 1) if total > 0 else 0

    # Non décrochés
    sql_nd = f"""
        SELECT COUNT(*) AS cnt FROM tb_reclamations
        WHERE {where} AND (LOWER(disponibilite_client) LIKE '%%joignable%%' OR LOWER(disponibilite_client) LIKE '%%sonne en vain%%')
    """
    non_decroche = query_db(sql_nd, params)[0]["cnt"] or 0
    pct_non_decroche = round((non_decroche / total * 100), 1) if total > 0 else 0

    # Délai moyen
    sql_delai = f"""
        SELECT AVG(EXTRACT(EPOCH FROM (date_traitement - date_assignation)) / 3600) AS delai
        FROM tb_reclamations
        WHERE {where} AND date_assignation IS NOT NULL AND date_traitement IS NOT NULL
    """
    delai_row = query_db(sql_delai, params)[0]
    delai_moyen = round(delai_row["delai"], 1) if delai_row["delai"] else 0

    # Par motif traité / non traité
    sql_motif = f"""
        SELECT
            COALESCE(motif_non_paiement, categorie_de_non_paiement, 'Non renseigné') AS motif,
            statut_traitement,
            COUNT(*) AS cnt
        FROM tb_reclamations WHERE {where}
        GROUP BY motif, statut_traitement
        ORDER BY cnt DESC
    """
    motif_rows = query_db(sql_motif, params)
    par_motif_traite = {}
    par_motif_non_traite = {}
    for r in motif_rows:
        m = r["motif"]
        if r["statut_traitement"] == "TRAITE":
            par_motif_traite[m] = r["cnt"]
        else:
            par_motif_non_traite[m] = par_motif_non_traite.get(m, 0) + r["cnt"]

    all_motifs = sorted(set(list(par_motif_traite.keys()) + list(par_motif_non_traite.keys())),
                        key=lambda m: par_motif_traite.get(m, 0) + par_motif_non_traite.get(m, 0), reverse=True)[:15]

    # Par jour
    sql_jour = f"""
        SELECT startdate::date AS jour, COUNT(*) AS cnt
        FROM tb_reclamations WHERE {where} AND startdate IS NOT NULL
        GROUP BY jour ORDER BY jour
    """
    jour_rows = query_db(sql_jour, params)
    par_jour = {str(r["jour"]): r["cnt"] for r in jour_rows}

    # Volume par agent
    sql_agent = f"""
        SELECT assigne_a,
            COUNT(*) AS total,
            SUM(CASE WHEN statut_traitement = 'TRAITE' THEN 1 ELSE 0 END) AS traite
        FROM tb_reclamations WHERE {where} AND assigne_a IS NOT NULL
        GROUP BY assigne_a
    """
    agent_rows = query_db(sql_agent, params)
    volume_par_agent = {}
    for r in agent_rows:
        volume_par_agent[r["assigne_a"]] = {
            "total": r["total"], "traite": r["traite"], "restant": r["total"] - r["traite"]
        }

    return {
        "total": total, "non_assignees": counts["non_assignees"] or 0,
        "assignees": counts["assignees"] or 0, "traitees": traitees, "taux": taux,
        "non_decroche": non_decroche, "pct_non_decroche": pct_non_decroche,
        "n1": counts["n1"] or 0, "n2": counts["n2"] or 0, "delai_moyen": delai_moyen,
        "motif_labels": all_motifs,
        "motif_traite_vals": [par_motif_traite.get(m, 0) for m in all_motifs],
        "motif_non_traite_vals": [par_motif_non_traite.get(m, 0) for m in all_motifs],
        "par_jour": par_jour,
        "volume_par_agent": volume_par_agent
    }


# ============================================================
# API - ACTIONS
# ============================================================
@app.post("/api/assigner-nombre")
async def api_assigner_nombre(request: Request):
    import traceback, logging
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)

    body = await request.json()
    motifs = body.get("motifs", [])
    categories = body.get("categories", [])
    campagnes_f = body.get("campagnes", [])
    nombre = body.get("nombre", 50)
    assigne_a = body.get("assigne_a")
    annee = body.get("annee", "")
    mois = body.get("mois", "")
    jour = body.get("jour", "")

    # Construire la requête SQL dynamique
    clauses = ["statut_traitement = 'NON ASSIGNE'"]
    params = []

    # Filtres date
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
        # Séparer les vrais motifs et le marqueur NULL
        real_motifs = [m for m in motifs if m != "__NULL__"]
        has_null = "__NULL__" in motifs
        sub_clauses = []
        if real_motifs:
            placeholders = ",".join(["%s"] * len(real_motifs))
            sub_clauses.append(f"motif_non_paiement IN ({placeholders})")
            params.extend(real_motifs)
        if has_null:
            sub_clauses.append("motif_non_paiement IS NULL")
        clauses.append("(" + " OR ".join(sub_clauses) + ")")
    if categories:
        real_cats = [c for c in categories if c != "__NULL__"]
        has_null = "__NULL__" in categories
        sub_clauses = []
        if real_cats:
            placeholders = ",".join(["%s"] * len(real_cats))
            sub_clauses.append(f"categorie_de_non_paiement IN ({placeholders})")
            params.extend(real_cats)
        if has_null:
            sub_clauses.append("categorie_de_non_paiement IS NULL")
        clauses.append("(" + " OR ".join(sub_clauses) + ")")
    if campagnes_f:
        placeholders = ",".join(["%s"] * len(campagnes_f))
        clauses.append(f"campagne IN ({placeholders})")
        params.extend(campagnes_f)

    where = " AND ".join(clauses)
    params.append(nombre)

    # ── #1 : transaction atomique + #3 : datetime sans isoformat ──
    sql_select = f"SELECT id_hash FROM tb_reclamations WHERE {where} ORDER BY startdate LIMIT %s"
    # sql_update_prefix : la fin "WHERE id_hash IN" est complétée dans execute_db_transaction
    sql_update_prefix = """UPDATE tb_reclamations
                    SET assigne_a = %s, statut_traitement = 'ASSIGNE', date_assignation = %s
                    WHERE id_hash IN"""
    try:
        rows = execute_db_transaction(
            sql_select, params,
            sql_update_prefix, [assigne_a, datetime.now()]
        )
        return {"assigned": len(rows)}
    except Exception as e:
        logging.error(f"DISPATCH ERROR: {e}")
        logging.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/desassigner")
async def api_desassigner(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)
    body = await request.json()
    execute_db(
        "UPDATE tb_reclamations SET assigne_a = NULL, statut_traitement = 'NON ASSIGNE', date_assignation = NULL WHERE id_hash = %s",
        [body["id_hash"]]
    )
    return {"success": True}


@app.post("/api/redistribuer")
async def api_redistribuer(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403)
    body = await request.json()

    sql = """SELECT id_hash FROM tb_reclamations
             WHERE assigne_a = %s AND statut_traitement != 'TRAITE'
             ORDER BY startdate LIMIT %s"""
    rows = query_db(sql, [body["agent_source"], body["nombre"]])

    if rows:
        ids = [r["id_hash"] for r in rows]
        placeholders = ",".join(["%s"] * len(ids))
        execute_db(
            f"UPDATE tb_reclamations SET assigne_a = %s, date_assignation = %s WHERE id_hash IN ({placeholders})",
            [body["agent_cible"], datetime.now().isoformat()] + ids
        )

    return {"redistributed": len(rows)}


@app.post("/api/traiter")
async def api_traiter(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    # ── #2 : validation backend statut_appel obligatoire ──
    if not body.get("statut_appel"):
        raise HTTPException(status_code=422, detail="Le statut de l'appel est obligatoire.")
    # ── #6 : vérifier que la réclamation appartient à l'utilisateur ──
    where_owner = "id_hash = %s"
    owner_params = [body["id_hash"]]
    if user["role"] == "utilisateur":
        where_owner += " AND assigne_a = %s"
        owner_params.append(user["login"])
    # ── #3 : datetime.now() sans isoformat() ──
    count = execute_db(
        f"""UPDATE tb_reclamations SET
            statut_appel = %s, motif_reel = %s, niveau = %s,
            commentaire_bo = %s, zone_client = %s, sous_motif = %s,
            id_dossier = %s, statut_traitement = 'TRAITE', date_traitement = %s
           WHERE {where_owner}""",
        [body.get("statut_appel"), body.get("motif_reel", ""), body.get("niveau"),
         body.get("commentaire_bo", ""), body.get("zone_client", ""),
         body.get("sous_motif", ""), body.get("id_dossier", ""),
         datetime.now()] + owner_params
    )
    if count == 0:
        raise HTTPException(status_code=403, detail="Action non autorisée sur cette réclamation.")
    return {"success": True}


@app.post("/api/modifier-commentaire")
async def api_modifier_commentaire(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    # ── #6 : vérifier que la réclamation appartient à l'utilisateur ──
    where_owner = "id_hash = %s"
    owner_params = [body["id_hash"]]
    if user["role"] == "utilisateur":
        where_owner += " AND assigne_a = %s"
        owner_params.append(user["login"])
    count = execute_db(
        f"UPDATE tb_reclamations SET commentaire_bo = %s WHERE {where_owner}",
        [body.get("commentaire_bo", "")] + owner_params
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
    annee: Optional[str] = None, mois: Optional[str] = None, jour: Optional[str] = None,
    statut: Optional[str] = None, motif: Optional[str] = None,
    categorie: Optional[str] = None, campagne: Optional[str] = None,
    date_assign_du: Optional[str] = None, date_assign_au: Optional[str] = None,
    date_trait_du: Optional[str] = None, date_trait_au: Optional[str] = None,
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    extra = {}
    if user["role"] == "utilisateur":
        extra["assigne_a"] = user["login"]
    if statut: extra["statut_traitement"] = statut
    if motif: extra["motif_non_paiement"] = motif
    if categorie: extra["categorie_de_non_paiement"] = categorie
    if campagne: extra["campagne"] = campagne

    where, params = build_where(annee, mois, jour, extra)
    if date_assign_du:
        where += " AND date_assignation >= %s"; params.append(date_assign_du)
    if date_assign_au:
        where += " AND date_assignation <= %s"; params.append(date_assign_au + " 23:59:59")
    if date_trait_du:
        where += " AND date_traitement >= %s"; params.append(date_trait_du)
    if date_trait_au:
        where += " AND date_traitement <= %s"; params.append(date_trait_au + " 23:59:59")
    data = query_db(f"SELECT * FROM tb_reclamations WHERE {where} ORDER BY startdate DESC", params)

    if not data:
        return JSONResponse({"error": "Aucune donnée"})

    # Convertir les datetimes — format YYYY-MM-DD pour les dates métier
    DATE_FIELDS = {"date_assignation", "date_traitement"}
    for row in data:
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.strftime('%Y-%m-%d') if k in DATE_FIELDS else v.isoformat()
            elif hasattr(v, 'isoformat'):
                row[k] = str(v)

    output = io.StringIO('\ufeff')  # BOM UTF-8 pour Excel Windows
    writer = csv.DictWriter(output, fieldnames=data[0].keys(), delimiter=";")
    writer.writeheader()
    writer.writerows(data)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=reclamations_{user['login']}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"}
    )
