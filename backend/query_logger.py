"""
Registro de consultas en SQLite.
Guarda cada query del chat para auditoría y control de uso por cliente.
"""
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.config import LOGS_DIR

logger = logging.getLogger(__name__)

DB_PATH = LOGS_DIR / "queries.db"

# Lock para escrituras concurrentes
_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    """Crea la tabla si no existe. Agrega columnas nuevas a tablas existentes."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queries (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    query       TEXT    NOT NULL,
                    filter      TEXT    NOT NULL DEFAULT 'all',
                    query_type  TEXT,
                    chunks      INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0,
                    client_id   TEXT    DEFAULT 'default',
                    ip          TEXT    DEFAULT ''
                )
            """)
            # Migración: agregar columna ip si la tabla ya existía sin ella
            existing = [r[1] for r in conn.execute("PRAGMA table_info(queries)").fetchall()]
            if "ip" not in existing:
                conn.execute("ALTER TABLE queries ADD COLUMN ip TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON queries(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_client    ON queries(client_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ip        ON queries(ip)")
            conn.commit()
            logger.info(f"Base de logs inicializada en {DB_PATH}")
        finally:
            conn.close()


def log_query(
    query: str,
    filter_collection: str,
    query_type: str,
    chunks_retrieved: int,
    duration_ms: int,
    client_id: str = "default",
    ip: str = "",
) -> None:
    """Registra una consulta en la base de datos."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                """INSERT INTO queries
                   (timestamp, query, filter, query_type, chunks, duration_ms, client_id, ip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, query, filter_collection, query_type, chunks_retrieved, duration_ms, client_id, ip),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Error registrando query: {e}")
        finally:
            conn.close()


def get_queries(
    limit: int = 100,
    offset: int = 0,
    client_id: Optional[str] = None,
    ip: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Retorna consultas paginadas con filtros opcionales."""
    conditions = []
    params: list = []

    if client_id:
        conditions.append("client_id = ?")
        params.append(client_id)
    if ip:
        conditions.append("ip = ?")
        params.append(ip)
    if date_from:
        conditions.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("timestamp <= ?")
        params.append(date_to + " 23:59:59")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = _get_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM queries {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM queries {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "queries": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def get_summary(client_id: Optional[str] = None) -> dict:
    """Resumen de uso: totales por día, tipo de consulta y cliente."""
    client_filter = "WHERE client_id = ?" if client_id else ""
    params = [client_id] if client_id else []

    conn = _get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM queries {client_filter}", params
        ).fetchone()[0]

        by_day = conn.execute(
            f"""SELECT substr(timestamp,1,10) as date, COUNT(*) as count
                FROM queries {client_filter}
                GROUP BY date ORDER BY date DESC LIMIT 30""",
            params,
        ).fetchall()

        by_type = conn.execute(
            f"""SELECT query_type, COUNT(*) as count
                FROM queries {client_filter}
                GROUP BY query_type ORDER BY count DESC""",
            params,
        ).fetchall()

        by_client = conn.execute(
            """SELECT client_id, COUNT(*) as count
               FROM queries GROUP BY client_id ORDER BY count DESC"""
        ).fetchall()

        by_ip = conn.execute(
            """SELECT ip, COUNT(*) as count
               FROM queries WHERE ip != ''
               GROUP BY ip ORDER BY count DESC LIMIT 50"""
        ).fetchall()

        avg_duration = conn.execute(
            f"SELECT AVG(duration_ms) FROM queries {client_filter}", params
        ).fetchone()[0]

        return {
            "total_queries": total,
            "avg_duration_ms": round(avg_duration or 0),
            "by_day": [dict(r) for r in by_day],
            "by_type": [dict(r) for r in by_type],
            "by_client": [dict(r) for r in by_client],
            "by_ip": [dict(r) for r in by_ip],
        }
    finally:
        conn.close()
