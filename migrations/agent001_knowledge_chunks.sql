-- ============================================================================
-- AGENT-001 — Modelo de Datos y Filtrado Híbrido en PostgreSQL (pgvector)
-- Store vectorial UNIFICADO: separa el Universo Normativo de los Datos
-- Transaccionales mediante la columna `domain`, con metadata indexada (JSONB)
-- para filtrado estructurado (operation_id, origin_country, etc.).
--
-- Requisitos: imagen pgvector/pgvector:pg16 (trae la extensión `vector`).
-- gen_random_uuid() es nativo desde PostgreSQL 13 — no requiere pgcrypto.
-- ============================================================================

-- 1. Extensión vectorial
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Tabla unificada de chunks
CREATE TABLE IF NOT EXISTS agentia_knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT NOT NULL,
    embedding   VECTOR(1536),                      -- text-embedding-3-small (OpenAI)
    domain      VARCHAR(50) NOT NULL CHECK (domain IN ('normative', 'transactional')),
    metadata    JSONB NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Índices (estrategia crítica para el VPS)

-- 3.a HNSW sobre embedding con métrica coseno: búsqueda vectorial veloz y de
--     bajo consumo. Se luce en búsquedas AMPLIAS (ej. domain='normative' sobre
--     muchas filas). m/ef_construction por defecto (16/64) — balance memoria/recall.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON agentia_knowledge_chunks
    USING hnsw (embedding vector_cosine_ops);

-- 3.b GIN dedicado a metadata con jsonb_path_ops: opclass optimizada exclusivamente
--     para el operador de contención @> (más compacta y selectiva que el jsonb_ops por
--     defecto). NO se incluye domain en el GIN: su baja cardinalidad hacía que el
--     planificador prefiriera el B-Tree de domain. Así el planificador asocia
--     `metadata @> '{...}'` directamente con este GIN por pura selectividad del JSONB.
CREATE INDEX IF NOT EXISTS idx_chunks_metadata_gin
    ON agentia_knowledge_chunks
    USING gin (metadata jsonb_path_ops);

-- 3.c B-Tree sobre domain: separación normative / transactional.
CREATE INDEX IF NOT EXISTS idx_chunks_domain
    ON agentia_knowledge_chunks (domain);
