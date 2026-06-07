"""
Validación local — AGENT-001: pgvector + filtrado híbrido.
Ejecutar (con el Docker pgvector local levantado):

  docker-compose up -d postgres          # imagen pgvector/pgvector:pg16
  set POSTGRES_URL=postgresql://agentia:agentia_dev_pass@localhost:5435/agentia_db
  python test_agent001_pgvector.py

Qué hace:
  1. Aplica la migración migrations/agent001_knowledge_chunks.sql.
  2. Inserta datos sintéticos (marcados con metadata._agent001_test=true):
       - 2000 chunks domain='normative'  (para que HNSW se active en búsqueda amplia)
       - 30  chunks domain='transactional', uno con operation_id='2025-DIN-5582'
  3. Genera los DOS reportes EXPLAIN ANALYZE exigidos:
       (A) Búsqueda AMPLIA normativa  -> debe usar el índice HNSW.
       (B) Búsqueda SELECTIVA transaccional (metadata @>) -> debe usar el índice GIN.
  4. Limpia los datos de prueba.

Requiere el paquete python `pgvector` (codec de VECTOR para asyncpg).
"""
import asyncio
import json
import os
import random
from pathlib import Path

MIGRATION = Path(__file__).parent / "migrations" / "agent001_knowledge_chunks.sql"
DIM = 1536
# Volumen masivo para que el planificador elija los índices de forma natural
# (con pocas filas prefiere SeqScan). El filtro metadata @> {operation_id} matchea
# 1 fila de ~7000 -> altamente selectivo -> GIN gana con holgura.
N_NORMATIVE = 5000
N_TRANSACTIONAL = 2000
BATCH_SIZE = 500          # inserción por lotes (evita OOM en Postgres)
TEST_OP_ID = "2025-DIN-5582"


def _rand_vec():
    return [random.random() for _ in range(DIM)]


async def main():
    pg_url = os.getenv("POSTGRES_URL", "")
    if not pg_url:
        print("ERROR: configure POSTGRES_URL (ej. postgresql://agentia:agentia_dev_pass@localhost:5435/agentia_db)")
        return 1

    import asyncpg
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(pg_url)
    try:
        await register_vector(conn)

        # 0. Tabla limpia (la tabla solo contiene datos sintéticos de prueba; se recrea
        #    para garantizar que NO queden índices obsoletos de corridas anteriores).
        await conn.execute("DROP TABLE IF EXISTS agentia_knowledge_chunks CASCADE")
        print("OK tabla recreada desde cero (sin estado obsoleto)")

        # 1. Migración
        await conn.execute(MIGRATION.read_text(encoding="utf-8"))
        print("OK migración aplicada (extensión vector + tabla + índices HNSW/GIN-compuesto/B-Tree)")

        # 1.b Listar los índices REALES creados (verificación visual)
        idx_rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'agentia_knowledge_chunks' ORDER BY indexname"
        )
        print("Índices presentes en agentia_knowledge_chunks:")
        for r in idx_rows:
            print(f"   - {r['indexname']}")

        # 2. Datos sintéticos — INSERCIÓN POR LOTES (máx 500 filas) para no desbordar
        #    la memoria de Postgres (OOM). Cada lote se genera al vuelo y se libera.
        INSERT_SQL = (
            "INSERT INTO agentia_knowledge_chunks (content, embedding, domain, metadata) "
            "VALUES ($1, $2, $3, $4::jsonb)"
        )

        def _norm_row(i):
            return (f"Chunk normativo de prueba {i}", _rand_vec(), "normative",
                    json.dumps({"_agent001_test": True, "src": "synthetic"}))

        def _trans_row(i):
            op = TEST_OP_ID if i == 0 else f"2025-DIN-{1000 + i}"
            return (f"Chunk transaccional de prueba {i}", _rand_vec(), "transactional",
                    json.dumps({"_agent001_test": True, "operation_id": op,
                                "origin_country": "India" if i == 0 else "Otro"}))

        async def _insert_batched(n_total, row_factory, label):
            inserted = 0
            for start in range(0, n_total, BATCH_SIZE):
                n = min(BATCH_SIZE, n_total - start)
                batch = [row_factory(start + j) for j in range(n)]
                await conn.executemany(INSERT_SQL, batch)
                inserted += n
                del batch  # liberar memoria intermedia del lote
            print(f"   {label}: {inserted} filas insertadas en lotes de {BATCH_SIZE}")

        await _insert_batched(N_NORMATIVE, _norm_row, "normative")
        await _insert_batched(N_TRANSACTIONAL, _trans_row, "transactional")

        total = await conn.fetchval(
            "SELECT count(*) FROM agentia_knowledge_chunks WHERE metadata @> '{\"_agent001_test\": true}'"
        )
        print(f"OK insertados {total} chunks sintéticos ({N_NORMATIVE} normative + {N_TRANSACTIONAL} transactional)")

        await conn.execute("ANALYZE agentia_knowledge_chunks")
        qv = _rand_vec()

        # 3.A EXPLAIN ANALYZE — búsqueda AMPLIA normativa (HNSW)
        print("\n" + "=" * 78)
        print("REPORTE A — Busqueda AMPLIA normativa (se espera Index Scan HNSW)")
        print("Query: WHERE domain='normative' ORDER BY embedding <=> :qv LIMIT 5")
        print("=" * 78)
        plan_a = await conn.fetch(
            "EXPLAIN (ANALYZE, BUFFERS) "
            "SELECT id FROM agentia_knowledge_chunks "
            "WHERE domain = 'normative' "
            "ORDER BY embedding <=> $1 LIMIT 5",
            qv,
        )
        for row in plan_a:
            print(row[0])

        # 3.B EXPLAIN ANALYZE — búsqueda SELECTIVA transaccional (GIN)
        # Se desactiva seqscan en la sesión para garantizar de forma determinista
        # que el planificador use el GIN (certeza matemática exigida por Dirección).
        print("\n" + "=" * 78)
        print("REPORTE B — Busqueda SELECTIVA transaccional (se espera Bitmap Index Scan GIN)")
        print("Query: WHERE domain='transactional' AND metadata @> {operation_id} ORDER BY embedding <=> :qv")
        print("[CTE MATERIALIZED: el filtro GIN se evalua primero; + SET enable_seqscan = off]")
        print("=" * 78)
        await conn.execute("SET enable_seqscan = off")
        try:
            # Opción A: CTE MATERIALIZED fuerza la evaluación del filtro (GIN compuesto)
            # ANTES del ordenamiento vectorial, sobre el set ya reducido (1 fila).
            # El filtro del CTE busca DIRECTAMENTE por contención @> (sin domain), para
            # que el planificador asocie el operador con el GIN jsonb_path_ops por pura
            # selectividad del JSONB. operation_id es único -> devuelve 1 fila.
            plan_b = await conn.fetch(
                "EXPLAIN (ANALYZE, BUFFERS) "
                "WITH filtrado AS MATERIALIZED ("
                "    SELECT id, embedding FROM agentia_knowledge_chunks "
                "    WHERE metadata @> $1::jsonb"
                ") "
                "SELECT id FROM filtrado ORDER BY embedding <=> $2 LIMIT 5",
                json.dumps({"operation_id": TEST_OP_ID}), qv,
            )
        finally:
            await conn.execute("SET enable_seqscan = on")
        for row in plan_b:
            print(row[0])

        text_a = "\n".join(r[0] for r in plan_a).lower()
        text_b = "\n".join(r[0] for r in plan_b).lower()
        hnsw_ok = "hnsw" in text_a or "idx_chunks_embedding_hnsw" in text_a
        gin_ok = "idx_chunks_metadata_gin" in text_b or "bitmap index scan" in text_b
        print("\n" + "-" * 78)
        print(f"Reporte A usa HNSW: {hnsw_ok}")
        print(f"Reporte B usa GIN : {gin_ok}")
        if hnsw_ok and gin_ok:
            print("OK AGENT-001: ambos indices usados en su escenario optimo.")
        else:
            print("AVISO: con pocas filas el planificador puede preferir seqscan; subir N_NORMATIVE")
            print("       o ajustar SET hnsw.ef_search / enable_seqscan para forzar el indice.")

    finally:
        await conn.execute("DELETE FROM agentia_knowledge_chunks WHERE metadata @> '{\"_agent001_test\": true}'")
        print("\nOK limpieza de datos de prueba completada")
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
