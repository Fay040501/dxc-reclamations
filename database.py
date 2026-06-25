"""
database.py — couche d'accès PostgreSQL
Corrections apportées :
  - Pool de connexions ThreadedConnectionPool (2-15) au lieu d'une connexion par requête
  - Sessions persistées en base (table dxc_active_sessions) au lieu du dict mémoire ACTIVE_SESSIONS
  - Logging structuré sur toutes les erreurs
  - Types explicites sur les signatures
"""
import logging
import os

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
}

# Pool global — initialisé au premier appel à get_pool()
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Retourne le pool, en le créant si nécessaire."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=15, **DB_CONFIG)
        logger.info("Pool PostgreSQL initialisé (2–15 connexions)")
    return _pool


def get_conn():
    """Emprunte une connexion du pool."""
    return get_pool().getconn()


def release_conn(conn) -> None:
    """Restitue une connexion au pool sans la fermer."""
    try:
        get_pool().putconn(conn)
    except Exception:
        pass  # Si le pool est fermé, on ignore


# ─────────────────────────────────────────────
# Sessions persistantes (remplacent ACTIVE_SESSIONS en mémoire)
# ─────────────────────────────────────────────

def init_sessions_table() -> None:
    """
    Crée la table dxc_active_sessions si elle n'existe pas.
    À appeler au startup de l'application.
    """
    sql = """
        CREATE TABLE IF NOT EXISTS dxc_active_sessions (
            login      TEXT PRIMARY KEY,
            token      TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        logger.info("Table dxc_active_sessions vérifiée/créée")
    except Exception as e:
        conn.rollback()
        logger.error(f"init_sessions_table — {e}")
        raise RuntimeError(f"Impossible d'initialiser la table de sessions : {e}") from e
    finally:
        release_conn(conn)


def session_set(login: str, token: str) -> None:
    """Enregistre ou remplace le token actif pour ce login (upsert)."""
    sql = """
        INSERT INTO dxc_active_sessions (login, token, created_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (login) DO UPDATE
            SET token = EXCLUDED.token, created_at = NOW()
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (login, token))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"session_set({login}) — {e}")
        raise RuntimeError(f"Erreur session_set : {e}") from e
    finally:
        release_conn(conn)


def session_get(login: str) -> str | None:
    """Retourne le token actif pour ce login, ou None s'il n'existe pas."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token FROM dxc_active_sessions WHERE login = %s", (login,)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"session_get({login}) — {e}")
        raise RuntimeError(f"Erreur session_get : {e}") from e
    finally:
        release_conn(conn)


def session_delete(login: str) -> None:
    """Supprime la session active pour ce login (logout)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM dxc_active_sessions WHERE login = %s", (login,)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"session_delete({login}) — {e}")
        raise RuntimeError(f"Erreur session_delete : {e}") from e
    finally:
        release_conn(conn)


# ─────────────────────────────────────────────
# Requêtes génériques
# ─────────────────────────────────────────────

def query_db(sql: str, params=None) -> list[dict]:
    """Exécute un SELECT et retourne une liste de dicts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"query_db — {e} | sql={sql[:120]}")
        raise RuntimeError(f"Erreur lecture base de données : {e}") from e
    finally:
        release_conn(conn)


def execute_db(sql: str, params=None) -> int:
    """Exécute un INSERT/UPDATE/DELETE et retourne le nombre de lignes affectées."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            count = cur.rowcount
        conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        logger.error(f"execute_db — {e} | sql={sql[:120]}")
        raise RuntimeError(f"Erreur écriture base de données : {e}") from e
    finally:
        release_conn(conn)


def execute_many(sql: str, params_list: list) -> int:
    """Exécute la même requête pour une liste de paramètres (batch)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, params_list, page_size=100)
            count = cur.rowcount
        conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        logger.error(f"execute_many — {e}")
        raise RuntimeError(f"Erreur batch base de données : {e}") from e
    finally:
        release_conn(conn)


def execute_db_transaction(
    sql_select: str,
    params_select: list,
    sql_update_prefix: str,
    params_update_prefix: list,
) -> list[dict]:
    """
    Transaction atomique SELECT + UPDATE dans la même connexion.
    Évite les race conditions lors du dispatch simultané.
    sql_update_prefix doit se terminer par 'WHERE id_hash IN'
    (sans les parenthèses — elles sont ajoutées ici dynamiquement).
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql_select, params_select)
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                ids = [r["id_hash"] for r in rows]
                placeholders = ",".join(["%s"] * len(ids))
                sql_update = sql_update_prefix + f" ({placeholders})"
                cur.execute(sql_update, params_update_prefix + ids)
        conn.commit()
        return rows
    except Exception as e:
        conn.rollback()
        logger.error(f"execute_db_transaction — {e}")
        raise RuntimeError(f"Erreur transaction dispatch : {e}") from e
    finally:
        release_conn(conn)
