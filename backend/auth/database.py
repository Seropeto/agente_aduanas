"""
Base de datos SQLite para usuarios y accesos demo.
"""
import sqlite3
import logging
from pathlib import Path
from backend.config import LOGS_DIR

logger = logging.getLogger(__name__)

AUTH_DB_PATH = LOGS_DIR / "auth.db"


def get_connection():
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_auth_db():
    """Crea las tablas si no existen y aplica migraciones aditivas."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                email       TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                company     TEXT DEFAULT '',
                password_hash TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'demo',
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                expires_at  TEXT
            )
        """)
        conn.commit()

        # Migración: campos de billing (aditivos, no rompen nada)
        billing_columns = [
            ("plan_id",       "TEXT    DEFAULT 'base'"),
            ("queries_used",  "INTEGER DEFAULT 0"),
            ("queries_limit", "INTEGER DEFAULT 960"),
            ("reset_date",    "TEXT    DEFAULT NULL"),
            ("extra_packs",   "INTEGER DEFAULT 0"),
            ("tipo_vps",      "TEXT    DEFAULT 'agentia'"),
            ("billing_status","TEXT    DEFAULT 'activo'"),
            ("alert_80_sent", "INTEGER DEFAULT 0"),
            ("subscription_start", "TEXT DEFAULT NULL"),
        ]
        existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col_name, col_def in billing_columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        conn.commit()

    logger.info("Base de datos de autenticación inicializada")


def get_user_by_email(email: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def create_user(
    user_id: str,
    email: str,
    name: str,
    company: str,
    password_hash: str,
    role: str,
    created_at: str,
    expires_at: str | None = None,
) -> dict:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO users
               (id, email, name, company, password_hash, role, is_active, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (user_id, email.lower(), name, company, password_hash, role, created_at, expires_at),
        )
        conn.commit()
    return get_user_by_id(user_id)


def admin_exists() -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        return row is not None


# ------------------------------------------------------------------ #
# BILLING — CRUD                                                       #
# ------------------------------------------------------------------ #

def update_password(user_id: str, new_password_hash: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_password_hash, user_id)
        )
        conn.commit()


def get_all_users() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role != 'admin' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user_plan(user_id: str, plan_id: str, queries_limit: int,
                     subscription_start: str, reset_date: str) -> dict | None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE users SET
                plan_id = ?, queries_limit = ?, queries_used = 0,
                extra_packs = 0, subscription_start = ?, reset_date = ?,
                alert_80_sent = 0, billing_status = 'activo'
            WHERE id = ?
        """, (plan_id, queries_limit, subscription_start, reset_date, user_id))
        conn.commit()
    return get_user_by_id(user_id)


def update_user_billing_status(user_id: str, billing_status: str, is_active: int) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET billing_status = ?, is_active = ? WHERE id = ?",
            (billing_status, is_active, user_id)
        )
        conn.commit()
    return get_user_by_id(user_id)


def update_user_info(user_id: str, name: str, company: str, tipo_vps: str) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET name = ?, company = ?, tipo_vps = ? WHERE id = ?",
            (name, company, tipo_vps, user_id)
        )
        conn.commit()
    return get_user_by_id(user_id)


def increment_query_count(user_id: str) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET queries_used = queries_used + 1 WHERE id = ?",
            (user_id,)
        )
        conn.commit()
    return get_user_by_id(user_id)


def mark_alert_sent(user_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET alert_80_sent = 1 WHERE id = ?", (user_id,)
        )
        conn.commit()


def add_extra_pack(user_id: str) -> dict:
    user = get_user_by_id(user_id)
    if not user:
        return {"ok": False, "reason": "Usuario no encontrado"}
    if (user.get("extra_packs") or 0) >= 2:
        return {"ok": False, "reason": "Máximo 2 paquetes adicionales por mes"}
    with get_connection() as conn:
        conn.execute("""
            UPDATE users SET
                extra_packs = extra_packs + 1,
                queries_limit = queries_limit + 200,
                billing_status = 'activo'
            WHERE id = ?
        """, (user_id,))
        conn.commit()
    return {"ok": True, "user": get_user_by_id(user_id)}


def get_users_needing_reset(today: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE reset_date IS NOT NULL AND reset_date <= ? AND role != 'admin'",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def reset_monthly_queries(user_id: str, next_reset_date: str) -> None:
    with get_connection() as conn:
        conn.execute("""
            UPDATE users SET
                queries_used = 0,
                extra_packs = 0,
                alert_80_sent = 0,
                reset_date = ?,
                billing_status = 'activo',
                is_active = 1
            WHERE id = ?
        """, (next_reset_date, user_id))
        conn.commit()
