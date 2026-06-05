"""
Módulo de base de datos PostgreSQL para metadatos de documentos internos.

Propósito (Ticket 001):
  - ChromaDB maneja SOLO la búsqueda semántica (vectores).
  - PostgreSQL maneja metadatos duros: fechas, tipos, user_id, rangos.
  - Multi-tenant obligatorio: toda consulta incluye WHERE user_id = $1.

Activación automática: si POSTGRES_URL está en el entorno, se activa el pool.
Si no está configurado, is_pg_enabled() devuelve False y las operaciones son no-ops.
Esto permite operar sin PostgreSQL en desarrollo o como fallback.
"""
import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

POSTGRES_URL: str = os.getenv("POSTGRES_URL", "")

# Reintentos al arrancar: cubre la race condition donde el contenedor de la app
# levanta antes de que el DNS/servicio de postgres esté resoluble (común en
# orquestadores como Coolify donde depends_on no siempre se honra).
PG_INIT_RETRIES = int(os.getenv("PG_INIT_RETRIES", "5"))
PG_INIT_DELAY_S = float(os.getenv("PG_INIT_DELAY_S", "3"))
# Reconexión en segundo plano si postgres tarda en aparecer (no bloquea el arranque).
PG_RECONNECT_INTERVAL_S = float(os.getenv("PG_RECONNECT_INTERVAL_S", "15"))
PG_BACKGROUND_MAX_TRIES = int(os.getenv("PG_BACKGROUND_MAX_TRIES", "40"))  # ~10 min

_pool = None        # asyncpg.Pool una vez inicializado


def is_pg_enabled() -> bool:
    """True si el pool está activo y operacional."""
    return _pool is not None


# ─── Ciclo de vida del pool ───────────────────────────────────────────────────

async def init_pool() -> None:
    """
    Inicializa el pool de conexiones asyncpg y aplica las migraciones DDL.
    Llamar desde el lifespan de FastAPI. No lanza excepción si PG no está disponible.
    """
    if not POSTGRES_URL:
        logger.info("POSTGRES_URL no configurado — PostgreSQL desactivado (modo ChromaDB solo)")
        return

    # Intentos iniciales (bloqueantes, breve): cubren el caso normal.
    for attempt in range(1, PG_INIT_RETRIES + 1):
        try:
            await _connect_once()
            logger.info(f"PostgreSQL pool inicializado correctamente (intento {attempt})")
            return
        except Exception as e:
            logger.warning(f"PostgreSQL no disponible (intento {attempt}/{PG_INIT_RETRIES}): {e}")
            if attempt < PG_INIT_RETRIES:
                await asyncio.sleep(PG_INIT_DELAY_S)

    # Si no conectó al arranque (postgres tarda en quedar resoluble en Coolify),
    # seguir reintentando en SEGUNDO PLANO sin bloquear el arranque del app.
    logger.error(
        f"PostgreSQL no disponible tras {PG_INIT_RETRIES} intentos iniciales — "
        f"reintentando en segundo plano cada {PG_RECONNECT_INTERVAL_S}s"
    )
    asyncio.create_task(_background_reconnect())


async def _connect_once() -> None:
    """Crea el pool y aplica migraciones. Lanza excepción si falla."""
    global _pool
    import asyncpg
    _pool = await asyncpg.create_pool(
        POSTGRES_URL,
        min_size=2,
        max_size=10,
        command_timeout=10,
    )
    try:
        await _run_migrations()
    except Exception:
        # Si las migraciones fallan, no dejar un pool a medias.
        await _pool.close()
        _pool = None
        raise


async def _background_reconnect() -> None:
    """
    Reintenta conectar a PostgreSQL en segundo plano hasta que postgres esté
    disponible (apenas Coolify lo registra en la red). Se detiene al conectar.
    """
    for attempt in range(1, PG_BACKGROUND_MAX_TRIES + 1):
        await asyncio.sleep(PG_RECONNECT_INTERVAL_S)
        if _pool is not None:
            return
        try:
            await _connect_once()
            logger.info(f"PostgreSQL conectado en segundo plano (intento background {attempt})")
            return
        except Exception as e:
            logger.warning(
                f"PostgreSQL aún no disponible (background {attempt}/{PG_BACKGROUND_MAX_TRIES}): {e}"
            )
    logger.error("PostgreSQL no se pudo conectar tras los reintentos en segundo plano — operando solo con ChromaDB")


async def close_pool() -> None:
    """Cierra el pool. Llamar al apagar la app."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool cerrado")


# ─── Migraciones DDL (idempotentes) ──────────────────────────────────────────

async def _run_migrations() -> None:
    """Crea tablas e índices si no existen. Seguro de re-ejecutar."""
    async with _pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                doc_id          TEXT NOT NULL UNIQUE,
                user_id         TEXT NOT NULL,
                title           TEXT NOT NULL,
                filename        TEXT NOT NULL DEFAULT '',
                content_type    TEXT NOT NULL DEFAULT 'documento',
                source          TEXT NOT NULL DEFAULT 'Documento interno',
                fecha_documento DATE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                total_chunks    INTEGER NOT NULL DEFAULT 0,
                tipo_documento  TEXT DEFAULT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS document_metadata (
                id          BIGSERIAL PRIMARY KEY,
                doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                user_id     TEXT NOT NULL,
                meta_key    TEXT NOT NULL,
                meta_value  TEXT,
                UNIQUE (doc_id, meta_key)
            )
        """)

        # Índices para las queries de filtrado del acceptance criteria
        for ddl in [
            "CREATE INDEX IF NOT EXISTS idx_docs_user    ON documents(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_docs_fecha   ON documents(user_id, fecha_documento)",
            "CREATE INDEX IF NOT EXISTS idx_docs_ctype   ON documents(user_id, content_type)",
            "CREATE INDEX IF NOT EXISTS idx_docmeta_user ON document_metadata(user_id, meta_key)",
        ]:
            await conn.execute(ddl)

    logger.info("Migraciones PostgreSQL aplicadas")


# ─── CRUD ─────────────────────────────────────────────────────────────────────

async def upsert_document(
    doc_id: str,
    user_id: str,
    title: str,
    filename: str = "",
    content_type: str = "documento",
    source: str = "Documento interno",
    fecha_documento: str | None = None,   # ISO YYYY-MM-DD
    total_chunks: int = 0,
    tipo_documento: str | None = None,
    extra_meta: dict[str, str] | None = None,
) -> None:
    """
    Inserta o actualiza un documento y sus metadatos extra.
    Idempotente (ON CONFLICT DO UPDATE).
    """
    if not is_pg_enabled():
        return

    fecha = None
    if fecha_documento:
        try:
            from datetime import date
            fecha = date.fromisoformat(fecha_documento)
        except ValueError:
            logger.warning(f"Fecha inválida ignorada: {fecha_documento!r}")

    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO documents
                    (doc_id, user_id, title, filename, content_type, source,
                     fecha_documento, total_chunks, tipo_documento)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (doc_id) DO UPDATE SET
                    title           = EXCLUDED.title,
                    filename        = EXCLUDED.filename,
                    content_type    = EXCLUDED.content_type,
                    source          = EXCLUDED.source,
                    fecha_documento = EXCLUDED.fecha_documento,
                    total_chunks    = EXCLUDED.total_chunks,
                    tipo_documento  = EXCLUDED.tipo_documento
                """,
                doc_id, user_id, title, filename, content_type, source,
                fecha, total_chunks, tipo_documento,
            )

            if extra_meta:
                for key, value in extra_meta.items():
                    await conn.execute(
                        """
                        INSERT INTO document_metadata (doc_id, user_id, meta_key, meta_value)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (doc_id, meta_key) DO UPDATE SET meta_value = EXCLUDED.meta_value
                        """,
                        doc_id, user_id, key, str(value),
                    )


async def delete_document_pg(doc_id: str, user_id: str) -> bool:
    """
    Elimina un documento y sus metadatos.
    AISLAMIENTO OBLIGATORIO: user_id garantiza que cada usuario solo borra los suyos.
    """
    if not is_pg_enabled():
        return False

    async with _pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM documents WHERE doc_id = $1 AND user_id = $2",
            doc_id, user_id,
        )
    return int(result.split()[-1]) > 0


async def get_document(doc_id: str, user_id: str) -> dict[str, Any] | None:
    """
    Obtiene un documento por doc_id.
    AISLAMIENTO OBLIGATORIO: user_id en el WHERE.
    """
    if not is_pg_enabled():
        return None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT doc_id, user_id, title, filename, content_type, source,
                   fecha_documento::text AS fecha_documento,
                   created_at::text AS created_at,
                   total_chunks, tipo_documento
            FROM documents
            WHERE doc_id = $1 AND user_id = $2
            """,
            doc_id, user_id,
        )
    return dict(row) if row else None


async def query_documents(
    user_id: str,
    content_type: str | None = None,
    fecha_desde: str | None = None,     # ISO YYYY-MM-DD
    fecha_hasta: str | None = None,     # ISO YYYY-MM-DD
    search_title: str | None = None,
    tipo_documento: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Query filtrada de documentos con AISLAMIENTO MULTI-TENANT OBLIGATORIO.

    La cláusula WHERE user_id = $1 es el PRIMER filtro — sin excepción.
    No existe path de código que permita consultar sin user_id.

    Ejemplo (acceptance criteria):
      query_documents(user_id, content_type='factura',
                      fecha_desde='2024-10-01', fecha_hasta='2025-03-31')
      → SQL: WHERE user_id = $1 AND content_type = $2
               AND fecha_documento >= $3::date AND fecha_documento <= $4::date
      Latencia esperada: < 10ms con idx_docs_fecha e idx_docs_ctype.
    """
    if not is_pg_enabled():
        return []

    conditions = ["user_id = $1"]
    params: list[Any] = [user_id]
    idx = 2

    if content_type:
        conditions.append(f"content_type = ${idx}"); params.append(content_type); idx += 1
    if tipo_documento:
        conditions.append(f"tipo_documento = ${idx}"); params.append(tipo_documento); idx += 1
    if fecha_desde:
        try:
            from datetime import date as _date
            conditions.append(f"fecha_documento >= ${idx}")
            params.append(_date.fromisoformat(fecha_desde))
            idx += 1
        except ValueError:
            logger.warning(f"fecha_desde inválida ignorada: {fecha_desde!r}")
    if fecha_hasta:
        try:
            from datetime import date as _date
            conditions.append(f"fecha_documento <= ${idx}")
            params.append(_date.fromisoformat(fecha_hasta))
            idx += 1
        except ValueError:
            logger.warning(f"fecha_hasta inválida ignorada: {fecha_hasta!r}")
    if search_title:
        conditions.append(f"title ILIKE ${idx}"); params.append(f"%{search_title}%"); idx += 1

    where = " AND ".join(conditions)
    sql = f"""
        SELECT doc_id, user_id, title, filename, content_type, source,
               fecha_documento::text AS fecha_documento,
               created_at::text AS created_at,
               total_chunks, tipo_documento
        FROM documents
        WHERE {where}
        ORDER BY fecha_documento DESC NULLS LAST, created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """
    params.extend([limit, offset])

    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [dict(r) for r in rows]


async def count_documents(user_id: str) -> int:
    """Cuenta total de documentos del usuario (para paginación)."""
    if not is_pg_enabled():
        return 0
    async with _pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE user_id = $1", user_id
        )


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

async def pg_status() -> dict[str, Any]:
    """Retorna estado del pool para el endpoint /health."""
    if not is_pg_enabled():
        return {"enabled": False, "status": "POSTGRES_URL no configurado"}
    try:
        async with _pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
        return {
            "enabled": True,
            "status": "ok",
            "pool_size": _pool.get_size(),
            "pool_free": _pool.get_idle_size(),
            "pg_version": version,
        }
    except Exception as e:
        return {"enabled": True, "status": f"error: {e}"}
