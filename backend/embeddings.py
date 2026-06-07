"""
Servicio de embeddings OpenAI (Sprint AGENT-002).

Genera embeddings de 1536 dimensiones con `text-embedding-3-small` para el store
vectorial unificado en pgvector (agentia_knowledge_chunks).

Configuración por entorno:
    OPENAI_API_KEY            — clave de API (obligatoria para que esté habilitado).
    OPENAI_EMBEDDING_MODEL    — modelo (default text-embedding-3-small, 1536 dims).

Degradación graceful: si no hay OPENAI_API_KEY, is_embeddings_enabled() == False y
las funciones lanzan RuntimeError explícito (el caller decide cómo manejarlo).
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = 1536

# Lote de textos por request a la API (OpenAI acepta listas; evita requests gigantes).
EMBED_BATCH = int(os.getenv("OPENAI_EMBED_BATCH", "100"))
EMBED_MAX_RETRIES = int(os.getenv("OPENAI_EMBED_RETRIES", "3"))

_client = None  # AsyncOpenAI lazy


def is_embeddings_enabled() -> bool:
    return bool(OPENAI_API_KEY)


def _get_client():
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY no configurada — servicio de embeddings deshabilitado.")
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Genera embeddings para una lista de textos. Procesa en lotes y reintenta ante
    errores transitorios. Retorna una lista de vectores (1536 floats cada uno),
    en el MISMO orden que los textos de entrada.
    """
    if not texts:
        return []
    client = _get_client()
    out: list[list[float]] = []

    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        last_err: Optional[Exception] = None
        for attempt in range(1, EMBED_MAX_RETRIES + 1):
            try:
                resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
                # La API garantiza el orden de salida = orden de entrada.
                out.extend([d.embedding for d in resp.data])
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning(f"[embeddings] error en lote (intento {attempt}/{EMBED_MAX_RETRIES}): {e}")
                if attempt < EMBED_MAX_RETRIES:
                    await asyncio.sleep(1.5 * attempt)
        if last_err is not None:
            raise last_err

    return out


async def embed_query(text: str) -> list[float]:
    """Embedding de una sola consulta (atajo)."""
    result = await embed_texts([text])
    return result[0] if result else []
