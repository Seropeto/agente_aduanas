"""
Validación local — AGENT-002: ingesta asíncrona /v1/ingest/transactional.
Ejecutar: python test_agent002_ingest.py

No requiere OpenAI ni PostgreSQL: se mockean embed_texts e insert_knowledge_chunks.
El parser (CSV) se ejecuta real sobre datos sintéticos.

Cubre los criterios de aceptación:
  1. El endpoint valida formato y responde 202 {status:queued} en < 200ms (delega a background).
  2. Formato no soportado -> 400.
  3. El worker parsea -> embeddings -> inserta con domain='transactional' + metadata correcta.
  4. Concurrencia: mientras el worker procesa (embedding lento), el event loop sigue
     respondiendo (heartbeat tickea) -> la API no se congela.
"""
import asyncio
import json
import time

import backend.embeddings as embeddings_mod
import backend.database as database_mod
from starlette.background import BackgroundTasks
from fastapi import HTTPException

CSV_BYTES = (
    "folio,descripcion,monto_usd,pais\n"
    "2025-DIN-5582,Instrumentos de medicion y control,16200,India\n"
    "2025-DIN-5583,Equipo optico,9000,India\n"
).encode("utf-8")


class FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data
    async def read(self):
        return self._data


async def test_endpoint_202_fast():
    from backend.main import ingest_transactional
    bt = BackgroundTasks()
    t0 = time.perf_counter()
    resp = await ingest_transactional(
        background_tasks=bt,
        file=FakeUpload("operacion.csv", CSV_BYTES),
        operation_id="2025-DIN-5582",
        origin_country="India",
        current_user={"id": "admin-test", "role": "admin"},
    )
    ms = (time.perf_counter() - t0) * 1000
    body = json.loads(bytes(resp.body).decode())
    assert resp.status_code == 202, resp.status_code
    assert body == {"status": "queued", "operation_id": "2025-DIN-5582"}, body
    assert len(bt.tasks) == 1, "debe encolar exactamente 1 tarea en background"
    assert ms < 200, f"el endpoint tardó {ms:.0f}ms (debe ser <200ms)"
    print(f"OK Test 1: 202 queued en {ms:.1f}ms (<200ms), 1 tarea en background")


async def test_format_rejected():
    from backend.main import ingest_transactional
    try:
        await ingest_transactional(
            background_tasks=BackgroundTasks(),
            file=FakeUpload("malo.txt", b"hola"),
            operation_id="X", origin_country="India",
            current_user={"id": "a", "role": "admin"},
        )
        assert False, "debió rechazar .txt"
    except HTTPException as e:
        assert e.status_code == 400
    print("OK Test 2: formato no soportado (.txt) -> 400")


async def test_worker_pipeline():
    from backend.main import _process_transactional_ingest

    captured = {}

    async def fake_embed(texts):
        return [[0.0] * 1536 for _ in texts]

    async def fake_insert(chunks, domain, batch_size=500):
        captured["chunks"] = chunks
        captured["domain"] = domain
        return len(chunks)

    embeddings_mod.embed_texts = fake_embed
    database_mod.insert_knowledge_chunks = fake_insert

    await _process_transactional_ingest(CSV_BYTES, "operacion.csv", "2025-DIN-5582", "India")

    assert captured.get("domain") == "transactional"
    assert len(captured.get("chunks", [])) >= 1
    meta = captured["chunks"][0]["metadata"]
    assert meta["operation_id"] == "2025-DIN-5582"
    assert meta["origin_country"] == "India"
    assert len(captured["chunks"][0]["embedding"]) == 1536
    assert "2025-DIN-5582" in captured["chunks"][0]["content"]  # cabecera de operación
    print(f"OK Test 3: worker -> {len(captured['chunks'])} chunks domain=transactional, metadata correcta")


async def test_concurrency_non_blocking():
    from backend.main import _process_transactional_ingest

    async def slow_embed(texts):
        await asyncio.sleep(2.0)  # simula embedding pesado (red/CPU)
        return [[0.0] * 1536 for _ in texts]

    async def noop_insert(chunks, domain, batch_size=500):
        return len(chunks)

    embeddings_mod.embed_texts = slow_embed
    database_mod.insert_knowledge_chunks = noop_insert

    ticks = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    t0 = time.perf_counter()
    worker = asyncio.create_task(
        _process_transactional_ingest(CSV_BYTES, "operacion.csv", "OP-X", "India")
    )
    await worker
    dur = time.perf_counter() - t0
    stop.set()
    await hb

    ticks_during = sum(1 for t in ticks if t <= t0 + dur)
    print(f"  worker tardó {dur*1000:.0f}ms (embedding lento); heartbeat ticks durante: {ticks_during}")
    assert dur >= 2.0, "el worker debió tardar ~2s por el embedding lento"
    assert ticks_during > 5, "el event loop se congeló durante el worker"
    print("OK Test 4: la API permanece responsiva mientras el worker procesa (loop NUNCA se congeló)")


async def main():
    print("=== AGENT-002 — Ingesta asíncrona /v1/ingest/transactional ===\n")
    await test_endpoint_202_fast()
    await test_format_rejected()
    await test_worker_pipeline()
    await test_concurrency_non_blocking()
    print("\n=== TODOS LOS TESTS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
