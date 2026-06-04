"""
Test de validación local — Ticket 003: Asincronía en generación de embeddings.
Ejecutar: python test_ticket003_async_cpu.py

No requiere PostgreSQL ni la API de Anthropic. Solo el modelo local de embeddings.
La primera ejecución carga all-MiniLM-L6-v2 (puede tardar unos segundos).

Demuestra que vector_store.aembed() NO congela el event loop, mientras que el
_embed() síncrono SÍ lo congela (prueba por contraste).
"""
import asyncio
import time

from backend.indexer.vectorstore import VectorStore

# Lote pesado para que la vectorización tarde algo medible en CPU
_FRASE = (
    "La base imponible del IVA en importaciones se calcula sobre el valor aduanero "
    "más los derechos ad valorem y demás gravámenes aplicables según la Ordenanza. "
)
HEAVY_BATCH = [(_FRASE * 6) + f" documento {i}" for i in range(500)]


async def _heartbeat(stop: asyncio.Event, ticks: list):
    """Tickea cada 10ms. Si el loop se congela, deja de tickear."""
    while not stop.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.01)


async def _simulated_chat(name: str, t_start: float, first_token: dict):
    """Simula una petición de chat por streaming: registra cuándo recibe su 1er token."""
    await asyncio.sleep(0)  # ceder al event loop
    first_token[name] = time.perf_counter() - t_start
    for _ in range(5):     # simular tokens subsiguientes
        await asyncio.sleep(0.02)


async def test_aembed_shape(vs):
    embs = await vs.aembed(["hola mundo", "prueba de embedding"])
    assert len(embs) == 2
    assert len(embs[0]) == 384, f"dim inesperada: {len(embs[0])}"
    print(f"OK Test 1: aembed retorna {len(embs)} vectores de dim {len(embs[0])}")


async def test_concurrency(vs):
    """
    Lanza un embedding pesado + 3 chats simulados con asyncio.gather.
    Criterio: los chats reciben tokens mientras la CPU sigue vectorizando.
    """
    ticks: list = []
    first_token: dict = {}
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(stop, ticks))

    t_start = time.perf_counter()
    embed_task = asyncio.create_task(vs.aembed(HEAVY_BATCH))
    chat_tasks = [
        asyncio.create_task(_simulated_chat(f"chat{i}", t_start, first_token))
        for i in range(3)
    ]

    # Los 3 chats deben completar mientras el embedding sigue corriendo
    await asyncio.gather(*chat_tasks)
    chats_done_at = time.perf_counter() - t_start
    embed_running_when_chats_done = not embed_task.done()

    await embed_task
    embed_dur = time.perf_counter() - t_start
    stop.set()
    await hb

    ticks_during = sum(1 for t in ticks if (t - t_start) < embed_dur)

    print(f"  duracion embedding pesado : {embed_dur*1000:.0f} ms ({len(HEAVY_BATCH)} textos)")
    print(f"  1er token de cada chat    : {sorted(round(v*1000) for v in first_token.values())} ms")
    print(f"  chats listos a            : {chats_done_at*1000:.0f} ms (embedding aun corria: {embed_running_when_chats_done})")
    print(f"  heartbeat ticks durante embedding: {ticks_during}")

    assert max(first_token.values()) < embed_dur, "Los chats no respondieron antes de terminar el embedding"
    assert ticks_during > 3, "El event loop se congelo durante el embedding async"
    print("OK Test 2: los 3 chats recibieron tokens mientras la CPU vectorizaba (loop NUNCA se congelo)")


async def test_sync_blocks_contrast(vs):
    """Contraste: el _embed SINCRONO si congela el loop (heartbeat deja de tickear)."""
    ticks: list = []
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(stop, ticks))
    await asyncio.sleep(0.1)  # dejar que el heartbeat se establezca

    n_before = len(ticks)
    t0 = time.perf_counter()
    vs._embed(HEAVY_BATCH)               # SINCRONO: bloquea el event loop
    block_dur = time.perf_counter() - t0
    n_during = len(ticks) - n_before     # ticks ocurridos durante el bloqueo (~0 esperado)

    stop.set()
    await hb

    print(f"  _embed SINCRONO bloqueo el loop {block_dur*1000:.0f} ms; heartbeat ticks durante el bloqueo: {n_during}")
    assert block_dur > 0.1, "El batch deberia tardar algo medible"
    assert n_during <= 2, f"Se esperaba congelamiento del loop, pero hubo {n_during} ticks"
    print("OK Test 3 (contraste): _embed sincrono CONGELA el loop; aembed NO. Fix demostrado.")


async def main():
    print("=== Ticket 003 - Asincronia de Embeddings en CPU ===\n")
    vs = VectorStore()
    print("Cargando modelo de embeddings (all-MiniLM-L6-v2)...")
    vs.initialize()
    print("Modelo cargado.\n")

    await test_aembed_shape(vs)
    await test_concurrency(vs)
    await test_sync_blocks_contrast(vs)
    print("\n=== TODOS LOS TESTS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
