import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "sslmode": os.getenv("DB_SSLMODE", "prefer")
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def query_db(sql, params=None):
    """Exécute une requête SELECT et retourne une liste de dicts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        raise RuntimeError(f"Erreur lecture base de données : {e}") from e
    finally:
        conn.close()


def execute_db(sql, params=None):
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
        raise RuntimeError(f"Erreur écriture base de données : {e}") from e
    finally:
        conn.close()


def execute_many(sql, params_list):
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
        raise RuntimeError(f"Erreur batch base de données : {e}") from e
    finally:
        conn.close()


def execute_db_transaction(sql_select, params_select, sql_update_prefix, params_update_prefix):
    """
    Transaction atomique pour le dispatch — évite les race conditions.
    sql_update_prefix : UPDATE ... SET ... WHERE id_hash IN
    Les placeholders IN (...) sont construits ici dynamiquement.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Étape 1 : sélectionner les IDs
            cur.execute(sql_select, params_select)
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                ids = [r["id_hash"] for r in rows]
                # Construire le SQL UPDATE en concaténation pure — pas de .format()
                placeholders = ",".join(["%s"] * len(ids))
                sql_update = sql_update_prefix + f" ({placeholders})"
                cur.execute(sql_update, params_update_prefix + ids)
        conn.commit()
        return rows
    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Erreur transaction dispatch : {e}") from e
    finally:
        conn.close()
