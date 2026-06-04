"""
Test de validación local — Ticket 002: SmartRouter (Haiku / Sonnet).
Ejecutar: python test_ticket002_router.py

Tests 1-3 y 4a corren sin dependencias externas (clasificador mockeado).
Test 4b (integración COMPLEX -> PostgreSQL) requiere PG del Ticket 001:
  docker-compose up -d postgres
  set POSTGRES_URL=postgresql://agentia:agentia_dev_pass@localhost:5435/agentia_db

El clasificador LLM se MOCKEA con un cliente async falso para que las pruebas
sean deterministas y no consuman tokens de la API.
"""
import asyncio
import os

from backend.rag.smart_router import (
    SmartRouter, MODEL_SIMPLE_STREAM, MODEL_COMPLEX_STREAM,
)


# ─── Cliente Anthropic falso (mock del clasificador) ──────────────────────────
class _FakeContent:
    def __init__(self, text): self.text = text

class _FakeResponse:
    def __init__(self, text): self.content = [_FakeContent(text)]

class _FakeMessages:
    def __init__(self, text): self._text = text
    async def create(self, **kwargs):
        return _FakeResponse(self._text)

class _FakeClient:
    def __init__(self, text): self.messages = _FakeMessages(text)

def fake_factory(text):
    """client_factory que produce un cliente cuyo clasificador responde `text`."""
    return lambda: _FakeClient(text)


def test_heuristica():
    r = SmartRouter(client_factory=None)
    assert r._heuristic_intent("¿Qué es el DFL 30?") == "SIMPLE"
    assert r._heuristic_intent("¿Cómo se calcula el IVA de una importación?") == "SIMPLE"
    assert r._heuristic_intent("Revisa las facturas entre octubre de 2025 y marzo de 2026") == "COMPLEX"
    assert r._heuristic_intent("Compara la clasificación de los Ray-Ban Meta") == "COMPLEX"
    print("OK Test 1: heurística clasifica SIMPLE/COMPLEX correctamente")


def test_filtros():
    r = SmartRouter(client_factory=None)
    f = r._extract_pg_filters("Revisa las facturas entre octubre de 2025 y marzo de 2026")
    assert f["content_type"] == "factura", f
    assert f["fecha_desde"] == "2025-10-01", f
    assert f["fecha_hasta"] == "2026-03-31", f
    print(f"OK Test 2: filtros extraídos = {f}")


async def test_classify_mock():
    r_simple = SmartRouter(client_factory=fake_factory('SIMPLE"}'))
    assert await r_simple.classify_intent("¿Qué es el DFL 30?") == "SIMPLE"
    r_complex = SmartRouter(client_factory=fake_factory('COMPLEX"}'))
    assert await r_complex.classify_intent("Revisa mis facturas") == "COMPLEX"
    print("OK Test 3: classify_intent (LLM mockeado) -> SIMPLE y COMPLEX")


async def test_route_simple():
    r = SmartRouter(client_factory=fake_factory('SIMPLE"}'))
    decision = await r.route("¿Qué es el DFL 30?", user_id="u-test")
    assert decision["intent"] == "SIMPLE"
    assert decision["model"] == MODEL_SIMPLE_STREAM, decision["model"]
    assert decision["pg_queried"] is False, "SIMPLE no debe tocar PostgreSQL"
    assert "haiku" in decision["model"].lower()
    print(f"OK Test 4a: SIMPLE -> modelo={decision['model']} pg_queried=False (sin tocar PostgreSQL)")


async def test_route_complex_pg():
    pg_url = os.getenv("POSTGRES_URL", "")
    if not pg_url:
        r = SmartRouter(client_factory=fake_factory('COMPLEX"}'))
        decision = await r.route("Revisa las facturas entre octubre de 2025 y marzo de 2026", user_id="u-test")
        assert decision["intent"] == "COMPLEX"
        assert decision["model"] == MODEL_COMPLEX_STREAM
        assert "sonnet" in decision["model"].lower()
        print(f"SKIP Test 4b (sin POSTGRES_URL): COMPLEX -> modelo={decision['model']} (Sonnet OK), pg_queried=False")
        return

    from backend.database import init_pool, close_pool, upsert_document, delete_document_pg
    await init_pool()

    uid = "router-test-user-002"
    await upsert_document(
        doc_id="router-test-factura-001",
        user_id=uid,
        title="Factura importación contenedor",
        filename="factura_cont.pdf",
        content_type="factura",
        fecha_documento="2025-11-20",
        total_chunks=3,
    )

    r = SmartRouter(client_factory=fake_factory('COMPLEX"}'))
    decision = await r.route(
        "Revisa las facturas entre octubre de 2025 y marzo de 2026", user_id=uid
    )

    assert decision["intent"] == "COMPLEX"
    assert decision["model"] == MODEL_COMPLEX_STREAM, decision["model"]
    assert "sonnet" in decision["model"].lower()
    assert decision["pg_queried"] is True, "COMPLEX debe interrogar PostgreSQL"
    assert decision["pg_filters"]["content_type"] == "factura"
    assert decision["pg_filters"]["fecha_desde"] == "2025-10-01"
    assert len(decision["pg_documents"]) >= 1
    assert decision["pg_documents"][0]["doc_id"] == "router-test-factura-001"
    print(f"OK Test 4b: COMPLEX -> modelo={decision['model']}, "
          f"PostgreSQL consultado en {pg_url.split('@')[-1]}, "
          f"{len(decision['pg_documents'])} factura(s) en rango Oct-Mar")

    await delete_document_pg("router-test-factura-001", uid)
    await close_pool()
    print("OK Test 4b: cleanup completado")


async def main():
    print("=== Ticket 002 — SmartRouter (Haiku / Sonnet) ===\n")
    test_heuristica()
    test_filtros()
    await test_classify_mock()
    await test_route_simple()
    await test_route_complex_pg()
    print("\n=== TODOS LOS TESTS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
