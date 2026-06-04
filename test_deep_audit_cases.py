"""
Test de validación local — Ticket 004-B: Auditoría profunda end-to-end.
Ejecutar: python test_deep_audit_cases.py

Estructura por etapas (cada una se activa según el entorno disponible):

  Etapa 1 (siempre): routing DETERMINISTA por heurística.
      Caso 1 (doctrinario)  -> SIMPLE / Haiku / pg_queried=False
      Caso 2 (auditoría)    -> COMPLEX / Sonnet

  Etapa 2 (si POSTGRES_URL en 5435): routing + extracción relacional real.
      Siembra DINs, enruta Caso 2, verifica que se consultó PostgreSQL y se
      extrajeron las DIN del user_id. Limpieza al final.

  Etapa 3 (si ANTHROPIC_API_KEY): BEST-EFFORT de contenido (no determinista).
      Llama al LLM real vía query_stream y reporta qué marcadores de calidad
      aparecieron (IVA 19%, RGI, origen/EUR.1, despachador/aforo, países).
      NOTA: la calidad depende de que la normativa real esté indexada
      (python cron_update_laws.py --seed-laws) y del modelo. No es un pass/fail
      garantizado de correctitud jurídica.
"""
import asyncio
import os

CASE1 = "¿Cómo se calcula el IVA en una factura de importación hoy de manera exacta?"
CASE2 = (
    "Para una empresa importadora chilena que tiene contratos, Audita la importación de equipos de medición de proveedores de España, India y "
    "Estados Unidos por USD 45.000, resuelve el conflicto de clasificación entre la "
    "partida 9026 y 9032, e indica la documentación de origen y el aforo requerido."
)


def stage1_routing_heuristic():
    from backend.rag.smart_router import SmartRouter
    r = SmartRouter(client_factory=None)  # heurística determinista
    i1 = r._heuristic_intent(CASE1)
    i2 = r._heuristic_intent(CASE2)
    assert i1 == "SIMPLE", f"Caso 1 deberia ser SIMPLE, fue {i1}"
    assert i2 == "COMPLEX", f"Caso 2 deberia ser COMPLEX, fue {i2}"
    print("OK Etapa 1: Caso 1 -> SIMPLE | Caso 2 -> COMPLEX (heuristica determinista)")


async def stage2_routing_pg():
    if not os.getenv("POSTGRES_URL", ""):
        print("SKIP Etapa 2: POSTGRES_URL no configurado (docker-compose up -d postgres + 5435)")
        return

    from backend.database import init_pool, close_pool, upsert_document, delete_document_pg
    from backend.rag.smart_router import SmartRouter, MODEL_SIMPLE_STREAM, MODEL_COMPLEX_STREAM
    from seed_demo_data import DEMO_DINS, _resolve_admin_user_id

    uid = _resolve_admin_user_id() or "deep-audit-test-user"
    await init_pool()
    for din in DEMO_DINS:
        await upsert_document(
            doc_id=din["doc_id"], user_id=uid, title=din["title"],
            content_type=din["content_type"], source="Carpeta de despacho (demo)",
            fecha_documento=din["fecha_documento"], total_chunks=1,
            tipo_documento="din", extra_meta=din["meta"],
        )

    try:
        r = SmartRouter(client_factory=None)  # heurística (determinista)

        d1 = await r.route(CASE1, uid)
        assert d1["intent"] == "SIMPLE"
        assert d1["model"] == MODEL_SIMPLE_STREAM
        assert d1["pg_queried"] is False, "Caso 1 (SIMPLE) NO debe tocar PostgreSQL"
        print(f"OK Etapa 2: Caso 1 -> {d1['model']} pg_queried=False")

        d2 = await r.route(CASE2, uid)
        assert d2["intent"] == "COMPLEX"
        assert d2["model"] == MODEL_COMPLEX_STREAM
        assert "sonnet" in d2["model"].lower()
        assert d2["pg_queried"] is True, "Caso 2 (COMPLEX) debe consultar PostgreSQL (5435)"
        assert len(d2["pg_documents"]) >= 3, f"Esperaba >=3 DIN, obtuvo {len(d2['pg_documents'])}"
        titulos = sorted(d["title"][:24] for d in d2["pg_documents"])
        print(f"OK Etapa 2: Caso 2 -> {d2['model']}, PostgreSQL devolvio {len(d2['pg_documents'])} DIN")
        print(f"           DIN extraidas: {titulos}")
    finally:
        for din in DEMO_DINS:
            await delete_document_pg(din["doc_id"], uid)
        await close_pool()
        print("OK Etapa 2: cleanup de DIN completado")


async def stage3_content_best_effort():
    if not os.getenv("ANTHROPIC_API_KEY", ""):
        print("SKIP Etapa 3: ANTHROPIC_API_KEY no configurada (best-effort de contenido)")
        return

    from backend.indexer.vectorstore import VectorStore
    from backend.rag.engine import RAGEngine

    vs = VectorStore(); vs.initialize()
    engine = RAGEngine(vs)

    async def _collect(query, user_id=None):
        import json
        text = ""
        async for ev in engine.query_stream(query=query, filter_collection="all", user_id=user_id):
            try:
                p = json.loads(ev.removeprefix("data: ").strip())
                if p.get("type") == "token":
                    text += p.get("text", "")
            except Exception:
                pass
        return text

    ans1 = await _collect(CASE1)
    markers1 = {
        "IVA 19%": "19" in ans1,
        "CIF/valor aduanero": ("cif" in ans1.lower() or "aduanero" in ans1.lower()),
        "ad valorem": "valorem" in ans1.lower(),
    }
    print(f"  [Caso 1] longitud respuesta={len(ans1)} | marcadores: {markers1}")

    ans2 = await _collect(CASE2)
    markers2 = {
        "RGI/regla interpretativa": ("rgi" in ans2.lower() or "regla general" in ans2.lower()),
        "origen/EUR.1": ("origen" in ans2.lower() or "eur.1" in ans2.lower() or "eur1" in ans2.lower()),
        "despachador/aforo": ("despachador" in ans2.lower() or "aforo" in ans2.lower()),
        "paises": sum(p in ans2.lower() for p in ("espana", "españa", "india", "estados unidos")) >= 2,
    }
    print(f"  [Caso 2] longitud respuesta={len(ans2)} | marcadores: {markers2}")

    assert len(ans1) > 0 and len(ans2) > 0, "El streaming no produjo texto"
    faltantes = [k for k, v in {**markers1, **markers2}.items() if not v]
    if faltantes:
        print(f"  AVISO Etapa 3 (best-effort): marcadores ausentes {faltantes}. "
              "Probable causa: normativa real no indexada (cron --seed-laws) o variabilidad del modelo.")
    print("OK Etapa 3: streaming completado (revision de calidad best-effort, ver marcadores arriba)")


async def main():
    print("=== Ticket 004-B - Auditoria Profunda End-to-End ===\n")
    stage1_routing_heuristic()
    await stage2_routing_pg()
    await stage3_content_best_effort()
    print("\n=== ETAPAS DETERMINISTAS PASARON (2 y 3 segun entorno) ===")


if __name__ == "__main__":
    asyncio.run(main())
