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
    """Crea las tablas si no existen."""
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
