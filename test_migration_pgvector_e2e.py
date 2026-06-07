"""
Validación END-TO-END de la migración ChromaDB → PostgreSQL/pgvector (AGENT-RELEASE-001).
Ejecutar:  python test_migration_pgvector_e2e.py

REQUISITOS (validación local antes de desplegar):
  PowerShell:
    $env:POSTGRES_URL = "postgresql://agentia:agentia_dev_pass@localhost:5435/agentia_db"
    $env:OPENAI_API_KEY = "sk-..."        # embeddings 1536 (obligatorio)
    $env:ANTHROPIC_API_KEY = "sk-ant-..." # opcional: habilita el test del engine completo
    python test_migration_pgvector_e2e.py

Si faltan POSTGRES o OPENAI, el script NO falla: imprime las instrucciones y sale 0.

Cubre el reemplazo total de ChromaDB:
  1. Migración: agentia_knowledge_chunks (+ dominio 'internal') y agentia_semantic_cache existen.
  2. Indexar normativa (OpenAI 1536) → búsqueda HNSW recupera el chunk relevante.
  3. Documento interno con user_id → filtro multi-tenant + búsqueda por título.
  4. Caché semántica en pgvector: store → HIT; consulta lejana → MISS.
  5. Stats por dominio, is_document_indexed, borrado por doc_id.
  6. (best-effort, con ANTHROPIC) engine.query sobre normativa sembrada → responde desde contexto (no NOT_FOUND).

Todo lo sembrado se marca metadata._mig_test='1' y se limpia al final.
"""
import asyncio
import os
import sys

MARK = {"_mig_test": "1"}
TEST_USER = "mig-test-user-0001"
DOC_NORM = "mig-test-norm-9026"
DOC_INT = "mig-test-int-decl"


def _preflight() -> bool:
    pg = os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_HOST")
    if not pg or not os.getenv("OPENAI_API_KEY"):
        print("WARN: validación OMITIDA — faltan variables de entorno.")
        print("  Configure POSTGRES_URL (o POSTGRES_HOST) y OPENAI_API_KEY y reejecute.")
        print("  Ver cabecera del archivo para el ejemplo PowerShell.")
        return False
    return True


async def _run() -> int:
    from backend.database import (
        init_pool, close_pool, is_pg_enabled,
        delete_chunks_by_meta, count_chunks_by_domain,
    )
    from backend.indexer.vectorstore import (
        VectorStore, COLLECTION_NORMATIVA, COLLECTION_INTERNOS, COLLECTION_CACHE,
    )

    await init_pool()
    if not is_pg_enabled():
        print("FALLO: no se pudo inicializar PostgreSQL. ¿Está arriba en :5435?")
        return 2

    vs = VectorStore()
    vs.initialize()
    failures = []

    try:
        # ── 1. Migración: tablas e índices creados por init_pool ──────────────
        from backend.database import pg as _pg
        async with _pg._pool.acquire() as conn:
            t1 = await conn.fetchval("SELECT to_regclass('public.agentia_knowledge_chunks')")
            t2 = await conn.fetchval("SELECT to_regclass('public.agentia_semantic_cache')")
            # ¿El dominio 'internal' es aceptado por el CHECK?
            internal_ok = await conn.fetchval(
                "SELECT 1 FROM information_schema.check_constraints "
                "WHERE check_clause LIKE '%internal%'"
            )
        assert t1 and t2, "faltan tablas pgvector"
        assert internal_ok, "el CHECK de domain no acepta 'internal'"
        print("OK 1: migración — agentia_knowledge_chunks + agentia_semantic_cache + dominio 'internal'")

        # ── 2. Normativa → búsqueda HNSW ──────────────────────────────────────
        norm_chunks = [
            {"text": "[Arancel] Partida 90.26: instrumentos para la MEDIDA de presión, caudal y nivel.",
             "metadata": {**MARK, "doc_id": DOC_NORM, "title": "Partida 90.26", "source": "Arancel"}},
            {"text": "[Arancel] Partida 90.32: instrumentos de REGULACIÓN o CONTROL AUTOMÁTICO con lazo de retroalimentación.",
             "metadata": {**MARK, "doc_id": DOC_NORM, "title": "Partida 90.32", "source": "Arancel"}},
            {"text": "[RGI] La Regla General Interpretativa 3 b) clasifica por el carácter esencial del artículo compuesto.",
             "metadata": {**MARK, "doc_id": DOC_NORM, "title": "RGI 3", "source": "Arancel"}},
        ]
        n = await vs.aadd_documents(norm_chunks, COLLECTION_NORMATIVA)
        assert n == 3, f"esperaba 3 chunks normativos, insertó {n}"
        hits = await vs.asearch("¿qué partida es para regulación o control automático?",
                                collection_name=COLLECTION_NORMATIVA, top_k=3)
        assert hits, "la búsqueda HNSW no devolvió resultados"
        top = hits[0]
        assert "90.32" in top["text"], f"el top no es 90.32: {top['text'][:60]}"
        assert top["collection"] == COLLECTION_NORMATIVA
        print(f"OK 2: normativa indexada (OpenAI 1536) — HNSW recupera 90.32 (dist={top['distance']:.4f})")

        # ── 3. Documento interno multi-tenant + búsqueda por título ───────────
        int_chunks = [
            {"text": "Declaración jurada SAG para importación de equipos de laboratorio.",
             "metadata": {**MARK, "doc_id": DOC_INT, "title": "declaracion-sag-660",
                          "filename": "declaracion-sag-660.pdf", "user_id": TEST_USER}},
        ]
        await vs.aadd_documents(int_chunks, COLLECTION_INTERNOS)
        # filtro multi-tenant
        mine = await vs.asearch("declaración jurada SAG", collection_name=COLLECTION_INTERNOS,
                                filter_metadata={"user_id": TEST_USER}, top_k=3)
        assert mine, "el filtro por user_id no devolvió el documento del usuario"
        other = await vs.asearch("declaración jurada SAG", collection_name=COLLECTION_INTERNOS,
                                 filter_metadata={"user_id": "otro-usuario"}, top_k=3)
        assert not other, "fuga multi-tenant: otro usuario ve el documento"
        # búsqueda por título (ILIKE en JSONB)
        by_title = await vs.aget_chunks_by_title_fragment("declaracion-sag-660",
                                                          COLLECTION_INTERNOS, user_id=TEST_USER)
        assert by_title and by_title[0]["distance"] == 0.0, "búsqueda por título falló"
        print("OK 3: documento interno — filtro multi-tenant estricto + búsqueda por título")

        # ── 4. Caché semántica en pgvector ────────────────────────────────────
        q = "¿Cuál es la tasa de IVA en importaciones de prueba migración?"
        await vs.acache_store(q, "El IVA es 19% (respuesta de prueba).", [{"title": "DL 825"}], "normativa")
        hit = await vs.acache_lookup(q)
        assert hit and hit.get("cache_hit") and "19%" in hit["answer"], "la caché no devolvió HIT"
        miss = await vs.acache_lookup("¿Cómo declarar un vehículo usado desde Japón en 2026?")
        assert miss is None, "la caché devolvió un falso positivo en consulta lejana"
        print("OK 4: caché semántica pgvector — HIT exacto + MISS en consulta lejana")

        # ── 5. Stats, existencia, borrado ─────────────────────────────────────
        stats = await vs.aget_stats()
        assert stats.get(COLLECTION_NORMATIVA, 0) >= 3 and stats.get(COLLECTION_INTERNOS, 0) >= 1
        assert await vs.ais_document_indexed(DOC_NORM, id_type="doc_id", collection_name=COLLECTION_NORMATIVA)
        assert not await vs.ais_document_indexed("no-existe-xyz", id_type="doc_id", collection_name=COLLECTION_NORMATIVA)
        removed = await vs.adelete_document(DOC_NORM, COLLECTION_NORMATIVA)
        assert removed, "el borrado por doc_id no eliminó nada"
        assert not await vs.ais_document_indexed(DOC_NORM, id_type="doc_id", collection_name=COLLECTION_NORMATIVA)
        print("OK 5: stats por dominio + is_document_indexed + borrado por doc_id")

        # ── 6. Engine completo (best-effort, requiere ANTHROPIC) ──────────────
        if os.getenv("ANTHROPIC_API_KEY"):
            # Reinsertar normativa (la borramos en el paso 5) para dar contexto al engine.
            await vs.aadd_documents(norm_chunks, COLLECTION_NORMATIVA)
            from backend.rag.engine import RAGEngine
            engine = RAGEngine(vs)
            res = await engine.query(
                "Según el arancel, ¿qué partida aplica a un instrumento de control automático con retroalimentación?",
                filter_collection="normativa",
            )
            ans = res.get("answer", "")
            assert res.get("total_chunks_retrieved", 0) > 0, "el engine no recuperó contexto desde pgvector"
            assert "no encontr" not in ans.lower() and "no dispongo" not in ans.lower(), \
                f"el engine respondió NOT_FOUND pese a haber contexto: {ans[:120]}"
            print(f"OK 6: engine.query end-to-end (pgvector→Claude) — {res['total_chunks_retrieved']} chunks, respuesta desde contexto")
        else:
            print("WARN 6: ANTHROPIC_API_KEY ausente — test del engine completo OMITIDO (best-effort)")

    except AssertionError as e:
        failures.append(str(e))
        print(f"FALLO: {e}")
    finally:
        # Limpieza: borra todo lo marcado _mig_test en ambos dominios + caché de prueba.
        try:
            for dom in ("normative", "internal"):
                await delete_chunks_by_meta("_mig_test", "1", dom)
            # las entradas de caché de prueba expiran por TTL; en local se pueden limpiar:
            await vs.aclear_collection(COLLECTION_CACHE)
        except Exception as e:
            print(f"(aviso) limpieza parcial: {e}")
        await close_pool()

    if failures:
        return 1
    print("\n=== MIGRACIÓN pgvector VALIDADA END-TO-END ===")
    return 0


def main() -> int:
    if not _preflight():
        return 0
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
