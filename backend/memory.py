"""
Memoria conversacional persistente por usuario.
Almacena el historial en SQLite e inyecta los turnos previos en las llamadas a Claude.
REQ-07: turnos recientes van verbatim; turnos antiguos se comprimen en resumen con Haiku.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from backend.auth.database import AUTH_DB_PATH

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20
RECENT_TURNS = int(os.getenv("MEMORY_RECENT_TURNS", "4"))   # interacciones recientes verbatim


def initialize_memory_db():
    """Crea las tablas conversation_memory y conversation_summary si no existen."""
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_memory (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                session_id   TEXT,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                sources      TEXT,
                created_at   TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_user "
            "ON conversation_memory(user_id, created_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summary (
                user_id    TEXT PRIMARY KEY,
                summary    TEXT NOT NULL,
                last_id    INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
    logger.info("Tablas de memoria conversacional inicializadas")


def get_history(user_id: str) -> list[dict[str, str]]:
    """
    Retorna el historial para inyectar en Claude:
      - Si hay más de RECENT_TURNS interacciones: [resumen_cacheado] + últimas RECENT_TURNS
      - Si no: todos los turnos disponibles
    """
    recent_limit = RECENT_TURNS * 2  # cada interacción = 2 filas (user + assistant)

    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        recent_rows = conn.execute(
            """
            SELECT role, content, created_at
            FROM conversation_memory
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, recent_limit),
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM conversation_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]

        summary_text = None
        if total > recent_limit:
            row = conn.execute(
                "SELECT summary FROM conversation_summary WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                summary_text = row["summary"]

    messages: list[dict] = []

    if summary_text:
        messages.append({
            "role": "user",
            "content": f"[RESUMEN DE CONVERSACIÓN PREVIA]\n{summary_text}",
        })
        messages.append({
            "role": "assistant",
            "content": "Entendido, recuerdo el contexto de nuestra conversación anterior.",
        })

    for row in reversed(recent_rows):
        date_prefix = f"[{row['created_at'][:10]}] " if row["role"] == "user" else ""
        messages.append({
            "role": row["role"],
            "content": f"{date_prefix}{row['content']}",
        })

    return messages


def needs_summary_update(user_id: str) -> bool:
    """Retorna True si hay turnos antiguos no cubiertos por el resumen actual."""
    recent_limit = RECENT_TURNS * 2
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM conversation_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]

        if total <= recent_limit:
            return False

        boundary = conn.execute(
            """
            SELECT id FROM conversation_memory
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (user_id, recent_limit),
        ).fetchone()

        if not boundary:
            return False

        summary = conn.execute(
            "SELECT last_id FROM conversation_summary WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not summary:
            return True

        return boundary[0] > summary[0]


def get_turns_to_summarize(user_id: str) -> tuple[list[dict], int | None]:
    """
    Retorna (lista de turnos a resumir, id del último turno incluido).
    Son todos los turnos excepto los últimos RECENT_TURNS*2.
    """
    recent_limit = RECENT_TURNS * 2
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, role, content FROM conversation_memory
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id FROM conversation_memory
                  WHERE user_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            ORDER BY id ASC
            """,
            (user_id, user_id, recent_limit),
        ).fetchall()

    if not rows:
        return [], None

    turns = [{"role": r["role"], "content": r["content"]} for r in rows]
    last_id = rows[-1]["id"]
    return turns, last_id


def save_summary(user_id: str, summary_text: str, last_id: int) -> None:
    """Guarda o actualiza el resumen cacheado del usuario."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO conversation_summary (user_id, summary, last_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                summary    = excluded.summary,
                last_id    = excluded.last_id,
                updated_at = excluded.updated_at
            """,
            (user_id, summary_text, last_id, now),
        )
        conn.commit()
    logger.info(f"Resumen de historial guardado (user={user_id[:8]}..., last_id={last_id})")


def save_turn(
    user_id: str,
    session_id: str,
    user_content: str,
    assistant_content: str,
    sources: list[dict] | None = None,
) -> None:
    """Persiste un turno completo (pregunta + respuesta) en la memoria."""
    now = datetime.now(timezone.utc).isoformat()
    sources_json = json.dumps(sources, ensure_ascii=False) if sources else None

    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.executemany(
            """
            INSERT INTO conversation_memory
                (user_id, session_id, role, content, sources, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (user_id, session_id, "user",      user_content,      None,         now),
                (user_id, session_id, "assistant",  assistant_content, sources_json, now),
            ],
        )
        conn.commit()


def delete_history(user_id: str) -> None:
    """Elimina todo el historial y el resumen de un usuario."""
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.execute("DELETE FROM conversation_memory WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM conversation_summary WHERE user_id = ?", (user_id,))
        conn.commit()
    logger.info(f"Historial eliminado para user_id={user_id}")
