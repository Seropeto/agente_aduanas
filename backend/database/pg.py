"""
Capa de acceso a PostgreSQL — núcleo de persistencia ÚNICO del sistema.

PostgreSQL (+ pgvector) maneja TODO:
  - Búsqueda semántica (agentia_knowledge_chunks: normativa, internos, transaccional).
  - Caché semántica (agentia_semantic_cache).
  - Metadatos duros de documentos (documents): fechas, tipos, user_id, rangos.
  - Multi-tenant: las consultas de documentos internos filtran por user_id.

Activación automática: si POSTGRES_HOST/POSTGRES_URL está en el entorno, se activa
el pool. Si no está configurado, is_pg_enabled() devuelve False y las operaciones
de retrieval/persistencia degradan a vacío (no hay backend alternativo).
"""
import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

POSTGRES_URL: str = os.getenv("POSTGRES_URL", "")

# Parámetros discretos (PREFERIDOS): evitan problemas de URL-encoding cuando la
# contraseña contiene caracteres especiales como '@', ':' o '/'. Si POSTGRES_HOST
# está definido, se conecta con estos parámetros y se IGNORA POSTGRES_URL (que
# podría venir mal armada por interpolación de una password con '@').
PG_HOST: str = os.getenv("POSTGRES_HOST", "")
PG_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB: str = os.getenv("POSTGRES_DB", "agentia_db")
PG_USER: str = os.getenv("POSTGRES_USER", "agentia")
PG_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "")


def _pg_configured() -> bool:
    """True si hay configuración suficiente para intentar conectar a PostgreSQL."""
    return bool(PG_HOST or POSTGRES_URL)


async def _register_pgvector(conn) -> None:
    """init callback del pool: registra el codec de VECTOR (pgvector) en cada conexión,
    permitiendo pasar/recibir embeddings como listas de floats en las queries."""
    try:
        from pgvector.asyncpg import register_vector
        await register_vector(conn)
    except Exception as e:
        logger.warning(f"No se pudo registrar el codec de pgvector: {e}")


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
    if not _pg_configured():
        logger.warning("PostgreSQL no configurado (sin POSTGRES_HOST ni POSTGRES_URL) — el retrieval quedará deshabilitado")
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
    """Crea el pool y aplica migraciones. Lanza excepción si falla.

    Prefiere parámetros discretos (host/user/password/db) — robustos ante
    contraseñas con caracteres especiales (@, :, /). Solo usa POSTGRES_URL si no
    hay POSTGRES_HOST configurado.
    """
    global _pool
    import asyncpg
    common = dict(min_size=2, max_size=10, command_timeout=10, init=_register_pgvector)
    if PG_HOST:
        _pool = await asyncpg.create_pool(
            host=PG_HOST, port=PG_PORT, database=PG_DB,
            user=PG_USER, password=PG_PASSWORD, **common,
        )
    else:
        _pool = await asyncpg.create_pool(POSTGRES_URL, **common)
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
    logger.error("PostgreSQL no se pudo conectar tras los reintentos en segundo plano — el retrieval quedará deshabilitado hasta reconectar")


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

        # ── Capa vectorial unificada (pgvector) ──────────────────────────────
        # Núcleo de persistencia ÚNICO: normativa, documentos internos y datos
        # transaccionales viven aquí. Reemplaza por completo a ChromaDB.
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agentia_knowledge_chunks (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                content     TEXT NOT NULL,
                embedding   VECTOR(1536),
                domain      VARCHAR(50) NOT NULL
                            CHECK (domain IN ('normative', 'transactional', 'internal')),
                metadata    JSONB NOT NULL,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Self-heal del CHECK de domain: CREATE TABLE IF NOT EXISTS NO altera una
        # tabla preexistente, así que una tabla creada en AGENT-001 conserva el
        # constraint viejo de 2 valores y rechaza 'internal'. Lo normalizamos con
        # un ALTER idempotente (DROP IF EXISTS + ADD) en cada arranque.
        await conn.execute(
            "ALTER TABLE agentia_knowledge_chunks "
            "DROP CONSTRAINT IF EXISTS agentia_knowledge_chunks_domain_check"
        )
        await conn.execute(
            "ALTER TABLE agentia_knowledge_chunks "
            "ADD CONSTRAINT agentia_knowledge_chunks_domain_check "
            "CHECK (domain IN ('normative', 'transactional', 'internal'))"
        )
        for ddl in [
            # HNSW para búsqueda vectorial amplia (coseno).
            "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
            "ON agentia_knowledge_chunks USING hnsw (embedding vector_cosine_ops)",
            # GIN dedicado a metadata (jsonb_path_ops) para el filtrado selectivo @>.
            "CREATE INDEX IF NOT EXISTS idx_chunks_metadata_gin "
            "ON agentia_knowledge_chunks USING gin (metadata jsonb_path_ops)",
            # B-Tree independiente para el dominio (baja cardinalidad).
            "CREATE INDEX IF NOT EXISTS idx_chunks_domain "
            "ON agentia_knowledge_chunks (domain)",
        ]:
            await conn.execute(ddl)

        # Caché semántica dedicada en pgvector (migrada desde la colección ChromaDB).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agentia_semantic_cache (
                id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                query_text  TEXT NOT NULL,
                embedding   VECTOR(1536) NOT NULL,
                answer      TEXT NOT NULL,
                sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
                query_type  VARCHAR(50) NOT NULL DEFAULT 'general',
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_embedding_hnsw "
            "ON agentia_semantic_cache USING hnsw (embedding vector_cosine_ops)"
        )

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


# ─── Inserción de chunks en agentia_knowledge_chunks (AGENT-002) ──────────────

async def insert_knowledge_chunks(
    chunks: list[dict[str, Any]],
    domain: str,
    batch_size: int = 500,
) -> int:
    """
    Inserta chunks en agentia_knowledge_chunks por LOTES (≤500) para no desbordar
    la memoria de Postgres (lección OOM de AGENT-001).

    Args:
        chunks: lista de {content: str, embedding: list[float], metadata: dict}.
        domain: 'normative' | 'transactional'.
    Returns:
        Número de filas insertadas.
    """
    if not is_pg_enabled() or not chunks:
        return 0
    if domain not in ("normative", "transactional", "internal"):
        raise ValueError(f"domain inválido: {domain}")

    sql = (
        "INSERT INTO agentia_knowledge_chunks (content, embedding, domain, metadata) "
        "VALUES ($1, $2, $3, $4::jsonb)"
    )
    inserted = 0
    async with _pool.acquire() as conn:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            rows = [
                (c["content"], c["embedding"], domain, json.dumps(c.get("metadata", {})))
                for c in batch
            ]
            await conn.executemany(sql, rows)
            inserted += len(rows)
    return inserted


# ─── Búsqueda híbrida en agentia_knowledge_chunks (AGENT-001) ─────────────────

async def search_chunks_hybrid(
    query_embedding: list[float],
    domain: str | None = None,
    metadata_filter: dict | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Filtrado híbrido: PRIMERO el filtro estructurado (domain + metadata @>) vía el
    índice GIN compuesto, dentro de una CTE MATERIALIZED, y LUEGO el ordenamiento
    vectorial (<=> coseno) sobre el set ya reducido. Esto evita que el planificador
    use fuerza bruta (B-Tree de domain + filtro de descarte) y garantiza el uso del
    GIN antes del cálculo de distancia.

    Args:
        query_embedding: vector de consulta (1536 dims).
        domain: 'normative' | 'transactional' | None.
        metadata_filter: dict para contención @> (ej. {"operation_id": "2025-DIN-5582"}).
        top_k: número de resultados.
    """
    if not is_pg_enabled():
        return []

    params: list[Any] = []
    idx = 1
    # El CTE prioriza el filtro SELECTIVO de metadata (GIN jsonb_path_ops). El domain
    # (baja cardinalidad) NO entra al GIN: se aplica como recheck EXTERNO sobre el set
    # ya reducido cuando hay metadata, o como filtro único (B-Tree) cuando no la hay.
    inner_conds: list[str] = []
    outer_conds: list[str] = []
    if metadata_filter:
        inner_conds.append(f"metadata @> ${idx}::jsonb")
        params.append(json.dumps(metadata_filter))
        idx += 1
        if domain:
            outer_conds.append(f"domain = ${idx}")
            params.append(domain)
            idx += 1
    elif domain:
        inner_conds.append(f"domain = ${idx}")
        params.append(domain)
        idx += 1

    inner_where = (" WHERE " + " AND ".join(inner_conds)) if inner_conds else ""
    outer_where = (" WHERE " + " AND ".join(outer_conds)) if outer_conds else ""

    qv_param = f"${idx}"
    params.append(query_embedding)
    idx += 1
    limit_param = f"${idx}"
    params.append(top_k)

    sql = f"""
        WITH filtrado AS MATERIALIZED (
            SELECT id, content, domain, metadata, embedding
            FROM agentia_knowledge_chunks
            {inner_where}
        )
        SELECT id, content, domain, metadata,
               (embedding <=> {qv_param}) AS distance
        FROM filtrado
        {outer_where}
        ORDER BY embedding <=> {qv_param}
        LIMIT {limit_param}
    """

    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


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


async def get_available_operations() -> dict[str, list[str]]:
    """
    Lista las operaciones y países efectivamente cargados en el dominio
    transaccional (para alimentar el protocolo de frustración elegante).

    Devuelve {"operations": [...], "countries": [...]} con valores distintos.
    """
    empty = {"operations": [], "countries": []}
    if not is_pg_enabled():
        return empty
    async with _pool.acquire() as conn:
        ops = await conn.fetch(
            "SELECT DISTINCT metadata->>'operation_id' AS op "
            "FROM agentia_knowledge_chunks "
            "WHERE domain = 'transactional' AND metadata->>'operation_id' IS NOT NULL "
            "ORDER BY op"
        )
        countries = await conn.fetch(
            "SELECT DISTINCT metadata->>'origin_country' AS c "
            "FROM agentia_knowledge_chunks "
            "WHERE domain = 'transactional' AND metadata->>'origin_country' IS NOT NULL "
            "ORDER BY c"
        )
    return {
        "operations": [r["op"] for r in ops if r["op"]],
        "countries": [r["c"] for r in countries if r["c"]],
    }


# ─── Soporte de retrieval del engine (reemplazo de ChromaDB) ──────────────────

async def delete_chunks_by_meta(field: str, value: str, domain: str | None = None) -> int:
    """Elimina chunks cuyo metadata[field] == value (opcionalmente acotado a un dominio)."""
    if not is_pg_enabled():
        return 0
    conds = ["metadata ->> $1 = $2"]
    params: list[Any] = [field, value]
    if domain:
        conds.append(f"domain = ${len(params) + 1}")
        params.append(domain)
    sql = "DELETE FROM agentia_knowledge_chunks WHERE " + " AND ".join(conds)
    async with _pool.acquire() as conn:
        result = await conn.execute(sql, *params)
    return int(result.split()[-1])


async def chunks_meta_exists(field: str, value: str, domain: str | None = None) -> bool:
    """True si existe al menos un chunk con metadata[field] == value."""
    if not is_pg_enabled():
        return False
    conds = ["metadata ->> $1 = $2"]
    params: list[Any] = [field, value]
    if domain:
        conds.append(f"domain = ${len(params) + 1}")
        params.append(domain)
    sql = "SELECT 1 FROM agentia_knowledge_chunks WHERE " + " AND ".join(conds) + " LIMIT 1"
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *params) is not None


async def count_chunks_by_domain() -> dict[str, int]:
    """Conteo de chunks por dominio (para estadísticas del scraper/UI)."""
    if not is_pg_enabled():
        return {}
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT domain, COUNT(*) AS n FROM agentia_knowledge_chunks GROUP BY domain"
        )
    return {r["domain"]: r["n"] for r in rows}


async def clear_chunks_by_domain(domain: str) -> int:
    """Vacía todos los chunks de un dominio (reset de normativa)."""
    if not is_pg_enabled():
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM agentia_knowledge_chunks WHERE domain = $1", domain
        )
    return int(result.split()[-1])


async def search_chunks_by_title(
    fragment: str, domain: str = "internal", user_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Búsqueda léxica por título/filename en metadata (ILIKE). Reemplaza el
    filtrado client-side de ChromaDB con un índice/scan en Postgres."""
    if not is_pg_enabled():
        return []
    like = f"%{fragment}%"
    conds = ["domain = $1", "(metadata ->> 'title' ILIKE $2 OR metadata ->> 'filename' ILIKE $2)"]
    params: list[Any] = [domain, like]
    if user_id:
        conds.append(f"metadata ->> 'user_id' = ${len(params) + 1}")
        params.append(user_id)
    params.append(limit)
    sql = (
        "SELECT content, metadata FROM agentia_knowledge_chunks WHERE "
        + " AND ".join(conds)
        + f" LIMIT ${len(params)}"
    )
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def list_internal_documents(user_id: str | None = None) -> list[dict[str, Any]]:
    """Lista documentos internos únicos (agrupados por doc_id) a partir de los
    chunks del dominio 'internal'. Equivale al list_documents de ChromaDB."""
    if not is_pg_enabled():
        return []
    where = "WHERE domain = 'internal'"
    params: list[Any] = []
    if user_id:
        where += " AND metadata ->> 'user_id' = $1"
        params.append(user_id)
    sql = f"""
        SELECT metadata ->> 'doc_id'       AS doc_id,
               MAX(metadata ->> 'title')        AS title,
               MAX(metadata ->> 'filename')     AS filename,
               MAX(metadata ->> 'date')         AS date,
               MAX(metadata ->> 'url')          AS url,
               MAX(metadata ->> 'content_type') AS content_type,
               MAX(metadata ->> 'source')       AS source,
               COUNT(*)                          AS total_chunks
        FROM agentia_knowledge_chunks
        {where}
        GROUP BY metadata ->> 'doc_id'
    """
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [
        {
            "doc_id": r["doc_id"] or "",
            "title": r["title"] or "Sin título",
            "filename": r["filename"] or "",
            "date": r["date"] or "",
            "url": r["url"] or "",
            "content_type": r["content_type"] or "",
            "source": r["source"] or "",
            "total_chunks": int(r["total_chunks"]),
        }
        for r in rows if r["doc_id"]
    ]


# ─── Caché semántica en pgvector (reemplazo de la colección ChromaDB) ─────────

async def cache_lookup_pg(
    query_embedding: list[float], threshold: float = 0.05, ttl_hours: float = 24.0,
) -> dict[str, Any] | None:
    """Devuelve la respuesta cacheada más cercana si su distancia coseno ≤ threshold
    y su antigüedad ≤ ttl_hours; si no, None."""
    if not is_pg_enabled():
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT answer, sources, query_type,
                   (embedding <=> $1) AS distance,
                   EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 AS age_hours
            FROM agentia_semantic_cache
            ORDER BY embedding <=> $1
            LIMIT 1
            """,
            query_embedding,
        )
    if not row or row["distance"] > threshold or row["age_hours"] > ttl_hours:
        return None
    sources = row["sources"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    return {
        "answer": row["answer"],
        "sources": sources or [],
        "query_type": row["query_type"],
        "cache_hit": True,
    }


async def cache_store_pg(
    query_text: str, embedding: list[float], answer: str, sources: list, query_type: str,
) -> None:
    """Almacena (o reemplaza) una respuesta en la caché semántica."""
    if not is_pg_enabled():
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM agentia_semantic_cache WHERE query_text = $1", query_text
            )
            await conn.execute(
                """
                INSERT INTO agentia_semantic_cache
                    (query_text, embedding, answer, sources, query_type)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                """,
                query_text, embedding, answer[:8000],
                json.dumps(sources, ensure_ascii=False), query_type,
            )


async def clear_semantic_cache_pg() -> int:
    """Vacía toda la caché semántica."""
    if not is_pg_enabled():
        return 0
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM agentia_semantic_cache")
    return int(result.split()[-1])


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
