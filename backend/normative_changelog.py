"""
Versionado y trazabilidad normativa (REQ-10).
Gestiona:
  - normative_changelog: historial de incorporaciones, modificaciones y derogaciones
  - document_hashes: hash de contenido por URL para detectar cambios entre scraping runs
"""
import calendar
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from backend.auth.database import AUTH_DB_PATH

logger = logging.getLogger(__name__)

VALID_CHANGE_TYPES = ("incorporacion", "modificacion", "derogacion")

_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


# ── Inicialización ─────────────────────────────────────────────────────────────

def initialize_changelog_db() -> None:
    """Crea las tablas normative_changelog y document_hashes si no existen."""
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS normative_changelog (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id         TEXT NOT NULL,
                sector              TEXT NOT NULL DEFAULT 'aduanas',
                title               TEXT NOT NULL,
                change_type         TEXT NOT NULL,
                change_date         TEXT NOT NULL,
                detected_date       TEXT NOT NULL,
                source_url          TEXT DEFAULT '',
                summary             TEXT DEFAULT '',
                content_hash        TEXT DEFAULT '',
                previous_version_id INTEGER,
                is_active           INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_changelog_date "
            "ON normative_changelog(change_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_changelog_sector "
            "ON normative_changelog(sector, change_date)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS document_hashes (
                url          TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                doc_id       TEXT NOT NULL,
                title        TEXT DEFAULT '',
                sector       TEXT DEFAULT 'aduanas',
                updated_at   TEXT NOT NULL
            )
        """)
        conn.commit()
    logger.info("Tablas de changelog normativo inicializadas")


# ── Hashes de documentos ───────────────────────────────────────────────────────

def get_stored_hash(url: str) -> Optional[dict]:
    """
    Retorna el registro almacenado para una URL dada, o None si no existe.
    Campos: url, content_hash, doc_id, title, sector, updated_at
    """
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM document_hashes WHERE url = ?", (url,)
        ).fetchone()
        return dict(row) if row else None


def upsert_document_hash(
    url: str,
    content_hash: str,
    doc_id: str,
    title: str = "",
    sector: str = "aduanas",
) -> None:
    """Inserta o actualiza el hash de contenido de un documento."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO document_hashes (url, content_hash, doc_id, title, sector, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                content_hash = excluded.content_hash,
                doc_id       = excluded.doc_id,
                title        = excluded.title,
                sector       = excluded.sector,
                updated_at   = excluded.updated_at
            """,
            (url, content_hash, doc_id, title, sector, now),
        )
        conn.commit()


# ── Changelog ──────────────────────────────────────────────────────────────────

def add_change(
    document_id: str,
    title: str,
    change_type: str,
    change_date: str,
    source_url: str = "",
    summary: str = "",
    content_hash: str = "",
    sector: str = "aduanas",
    previous_version_id: Optional[int] = None,
) -> int:
    """
    Inserta una entrada en el changelog.
    Retorna el id de la fila insertada.
    """
    if change_type not in VALID_CHANGE_TYPES:
        change_type = "incorporacion"

    detected = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(AUTH_DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO normative_changelog
                (document_id, sector, title, change_type, change_date,
                 detected_date, source_url, summary, content_hash,
                 previous_version_id, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (document_id, sector, title, change_type, change_date,
             detected, source_url, summary, content_hash,
             previous_version_id),
        )
        conn.commit()
        return cur.lastrowid


def get_changes_by_period(
    from_date: date,
    to_date: date,
    sector: Optional[str] = None,
    change_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Retorna cambios en el rango [from_date, to_date], ordenados por change_date DESC.
    """
    query  = "SELECT * FROM normative_changelog WHERE change_date BETWEEN ? AND ?"
    params: list = [from_date.isoformat(), to_date.isoformat()]

    if sector:
        query += " AND sector = ?"
        params.append(sector)
    if change_type and change_type in VALID_CHANGE_TYPES:
        query += " AND change_type = ?"
        params.append(change_type)

    query += " ORDER BY change_date DESC, detected_date DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_recent_changes(days: int = 7, sector: Optional[str] = None) -> list[dict]:
    """Atajo para obtener cambios de los últimos N días."""
    to_d   = date.today()
    from_d = to_d - timedelta(days=days)
    return get_changes_by_period(from_d, to_d, sector=sector)


def get_changelog_page(
    limit: int = 50,
    offset: int = 0,
    sector: Optional[str] = None,
    change_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Paginación del changelog para el panel admin. Retorna {items, total, limit, offset}."""
    base   = "FROM normative_changelog WHERE 1=1"
    params: list = []

    if sector:
        base += " AND sector = ?"
        params.append(sector)
    if change_type and change_type in VALID_CHANGE_TYPES:
        base += " AND change_type = ?"
        params.append(change_type)
    if date_from:
        base += " AND change_date >= ?"
        params.append(date_from)
    if date_to:
        base += " AND change_date <= ?"
        params.append(date_to)

    with sqlite3.connect(AUTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows  = conn.execute(
            f"SELECT * {base} ORDER BY detected_date DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ── Resolución de periodos en lenguaje natural ─────────────────────────────────

_TEMPORAL_TRIGGERS = [
    "qué cambió", "que cambio", "qué cambia", "que cambia",
    "cambios recientes", "novedades", "actualizaciones recientes",
    "normativa reciente", "circulares recientes", "resoluciones recientes",
    "modificaciones recientes", "últimas modificaciones", "ultimas modificaciones",
    "últimas novedades", "ultimas novedades",
]


def parse_temporal_query(query: str) -> Optional[tuple[date, date, str]]:
    """
    Detecta si la consulta incluye una referencia temporal y la convierte en
    un rango de fechas.

    Retorna (from_date, to_date, descripcion_legible) o None si no hay
    intención temporal.
    """
    q   = query.lower()
    hoy = date.today()

    has_trigger = any(t in q for t in _TEMPORAL_TRIGGERS)

    # Hoy
    if re.search(r"\bhoy\b", q) and "ayer" not in q:
        return hoy, hoy, "hoy"

    # Ayer
    if re.search(r"\bayer\b", q):
        ayer = hoy - timedelta(days=1)
        return ayer, ayer, "ayer"

    # Esta semana
    if re.search(r"\besta\s+semana\b", q):
        lunes = hoy - timedelta(days=hoy.weekday())
        return lunes, hoy, "esta semana"

    # Semana pasada
    if re.search(r"\b(la\s+)?semana\s+pasada\b", q):
        lunes  = hoy - timedelta(days=hoy.weekday() + 7)
        return lunes, lunes + timedelta(days=6), "la semana pasada"

    # Este mes
    if re.search(r"\beste\s+mes\b", q):
        return hoy.replace(day=1), hoy, "este mes"

    # Mes pasado / último mes
    if re.search(r"\b(el\s+)?mes\s+pasado\b", q) or re.search(r"\b[uú]ltimo\s+mes\b", q):
        primer_actual = hoy.replace(day=1)
        ultimo_pasado = primer_actual - timedelta(days=1)
        return ultimo_pasado.replace(day=1), ultimo_pasado, "el mes pasado"

    # Este año
    if re.search(r"\beste\s+a[ñn]o\b", q):
        return date(hoy.year, 1, 1), hoy, f"este año ({hoy.year})"

    # Año pasado / último año
    if re.search(r"\b(el\s+)?a[ñn]o\s+pasado\b", q) or re.search(r"\b[uú]ltimo\s+a[ñn]o\b", q):
        anio = hoy.year - 1
        return date(anio, 1, 1), date(anio, 12, 31), f"el año {anio}"

    # Hace N días / semanas / meses
    m = re.search(r"\bhace\s+(\d+)\s+(d[ií]as?|semanas?|meses?)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(weeks=n) if "sem" in unit else (timedelta(days=n * 30) if "mes" in unit else timedelta(days=n))
        return hoy - delta, hoy, f"hace {n} {m.group(2)}"

    # Hace una semana / un mes
    if re.search(r"\bhace\s+una?\s+semana\b", q):
        return hoy - timedelta(weeks=1), hoy, "la última semana"
    if re.search(r"\bhace\s+un\s+mes\b", q):
        return hoy - timedelta(days=30), hoy, "el último mes"

    # Últimos N días / semanas
    m = re.search(r"\b[uú]ltimos?\s+(\d+)\s+(d[ií]as?|semanas?)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(weeks=n) if "sem" in unit else timedelta(days=n)
        return hoy - delta, hoy, f"los últimos {n} {m.group(2)}"

    # En [mes] [de año]
    pat_mes = r"\ben\s+(" + "|".join(_MESES_ES.keys()) + r")(?:\s+de\s+(\d{4}))?\b"
    m = re.search(pat_mes, q)
    if m:
        mes_num = _MESES_ES[m.group(1)]
        anio    = int(m.group(2)) if m.group(2) else hoy.year
        if anio == hoy.year and mes_num > hoy.month:
            anio -= 1
        _, ultimo_dia = calendar.monthrange(anio, mes_num)
        return date(anio, mes_num, 1), date(anio, mes_num, ultimo_dia), f"{m.group(1)} de {anio}"

    # En [año] / año [año]
    m = re.search(r"\b(?:en\s+|a[ñn]o\s+)?(\d{4})\b", q)
    if m:
        anio = int(m.group(1))
        if 2010 <= anio <= hoy.year:
            fin = date(anio, 12, 31) if anio < hoy.year else hoy
            return date(anio, 1, 1), fin, f"el año {anio}"

    # Trigger genérico → últimos 30 días
    if has_trigger:
        return hoy - timedelta(days=30), hoy, "los últimos 30 días"

    return None
