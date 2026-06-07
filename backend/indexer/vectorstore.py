"""
Almacenamiento vectorial unificado sobre PostgreSQL + pgvector.

Reemplaza por completo a ChromaDB. Todo el conocimiento (normativa, documentos
internos del usuario y caché semántica) vive en PostgreSQL:

  - normativa_aduanera  → agentia_knowledge_chunks (domain='normative')
  - documentos_internos → agentia_knowledge_chunks (domain='internal')
  - cache_consultas     → agentia_semantic_cache

Los embeddings se generan con OpenAI (text-embedding-3-small, 1536 dims) de forma
asíncrona. Todos los métodos de I/O son async-native (no usan hilos): el pool de
asyncpg vive en el event loop principal, por lo que el retrieval corre directo
sobre el loop sin el salto a `to_thread` que requería ChromaDB/SentenceTransformer.

Las constantes COLLECTION_* se conservan como etiquetas lógicas para no romper a
los llamadores; internamente mapean a un `domain` de la tabla unificada.
"""
import hashlib
import json
import logging
from typing import Any, Optional

from backend.embeddings import embed_query, embed_texts, is_embeddings_enabled
from backend.database import (
    insert_knowledge_chunks, search_chunks_hybrid, search_chunks_by_title,
    delete_chunks_by_meta, chunks_meta_exists, count_chunks_by_domain,
    clear_chunks_by_domain, list_internal_documents,
    cache_lookup_pg, cache_store_pg, clear_semantic_cache_pg, is_pg_enabled,
)

logger = logging.getLogger(__name__)

# Etiquetas lógicas (compatibilidad con los llamadores) → dominio en la tabla unificada.
COLLECTION_NORMATIVA = "normativa_aduanera"
COLLECTION_INTERNOS = "documentos_internos"
COLLECTION_CACHE = "cache_consultas"

_COLLECTION_TO_DOMAIN = {
    COLLECTION_NORMATIVA: "normative",
    COLLECTION_INTERNOS: "internal",
}
_DOMAIN_TO_COLLECTION = {v: k for k, v in _COLLECTION_TO_DOMAIN.items()}

CACHE_DISTANCE_THRESHOLD = 0.05   # similitud coseno ≥ 0.95 (estricto)
CACHE_TTL_HOURS = 24


def _domain(collection_name: str) -> str:
    try:
        return _COLLECTION_TO_DOMAIN[collection_name]
    except KeyError:
        raise ValueError(f"Colección desconocida: {collection_name}")


def _as_dict(metadata: Any) -> dict:
    """asyncpg puede devolver JSONB como str — normaliza a dict."""
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except json.JSONDecodeError:
            return {}
    return metadata or {}


def _map_row(row: dict, distance_override: float | None = None) -> dict:
    """Normaliza una fila de agentia_knowledge_chunks al formato que espera el engine."""
    distance = distance_override if distance_override is not None else float(row.get("distance", 1.0))
    domain = row.get("domain")
    return {
        "text": row.get("content", ""),
        "metadata": _as_dict(row.get("metadata")),
        "distance": distance,
        "collection": _DOMAIN_TO_COLLECTION.get(domain, domain),
        "relevance_score": 1.0 - distance,
    }


class VectorStore:
    """Interfaz de alto nivel sobre PostgreSQL + pgvector (async-native)."""

    def __init__(self):
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Ciclo de vida (no-op: el pool lo gestiona backend.database.init_pool) #
    # ------------------------------------------------------------------ #
    def initialize(self):
        """Compatibilidad. El pool de PostgreSQL se inicializa en el lifespan
        de FastAPI vía init_pool(); aquí no hay estado propio que arrancar."""
        self._initialized = True
        logger.info("VectorStore (pgvector) listo — backend único PostgreSQL")

    def close(self):
        logger.info("VectorStore cerrado")

    # ------------------------------------------------------------------ #
    # Embeddings (OpenAI)                                                  #
    # ------------------------------------------------------------------ #
    async def _aembed_one(self, text: str) -> list[float] | None:
        if not is_embeddings_enabled():
            logger.warning("OPENAI_API_KEY no configurada — embeddings deshabilitados")
            return None
        try:
            return await embed_query(text)
        except Exception as e:
            logger.error(f"Error generando embedding de consulta: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Inserción                                                            #
    # ------------------------------------------------------------------ #
    async def aadd_documents(
        self, chunks: list[dict[str, Any]], collection_name: str = COLLECTION_NORMATIVA,
    ) -> int:
        """Embebe (OpenAI) e inserta chunks en el dominio correspondiente."""
        if not chunks:
            return 0
        if not is_embeddings_enabled():
            logger.error("No se puede indexar: OPENAI_API_KEY no configurada")
            return 0
        domain = _domain(collection_name)

        prepared, texts = [], []
        for chunk in chunks:
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            meta = dict(chunk.get("metadata", {}))
            doc_id = meta.get("doc_id") or hashlib.sha256(text.encode()).hexdigest()[:16]
            meta.setdefault("doc_id", doc_id)
            meta.setdefault("source", "desconocido")
            meta.setdefault("title", "Sin título")
            meta.setdefault("date", "")
            meta.setdefault("url", "")
            meta.setdefault("content_type", "normativa" if domain == "normative" else "documento")
            prepared.append(meta)
            texts.append(text)

        if not texts:
            return 0

        try:
            embeddings = await embed_texts(texts)
        except Exception as e:
            logger.error(f"Error generando embeddings para indexar: {e}")
            return 0

        records = [
            {"content": texts[i], "embedding": embeddings[i], "metadata": prepared[i]}
            for i in range(len(texts))
        ]
        inserted = await insert_knowledge_chunks(records, domain)
        logger.info(f"Insertados {inserted} chunks en dominio '{domain}'")
        return inserted

    # ------------------------------------------------------------------ #
    # Búsqueda                                                             #
    # ------------------------------------------------------------------ #
    async def asearch(
        self, query: str, collection_name: str | None = None,
        top_k: int = 3, filter_metadata: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Búsqueda semántica (coseno) en uno o ambos dominios."""
        if not query.strip() or not is_pg_enabled():
            return []
        embedding = await self._aembed_one(query)
        if embedding is None:
            return []

        if collection_name in (COLLECTION_NORMATIVA, COLLECTION_INTERNOS):
            domains = [_domain(collection_name)]
        else:
            domains = ["normative", "internal"]

        results: list[dict] = []
        for domain in domains:
            try:
                rows = await search_chunks_hybrid(
                    query_embedding=embedding, domain=domain,
                    metadata_filter=filter_metadata, top_k=top_k,
                )
                results.extend(_map_row(r) for r in rows)
            except Exception as e:
                logger.error(f"Error buscando en dominio '{domain}': {e}")

        results.sort(key=lambda x: x["distance"])
        return results

    async def aget_chunks_by_title_fragment(
        self, title_fragment: str, collection_name: str = COLLECTION_INTERNOS,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Devuelve chunks cuyo título/filename contenga el fragmento (ILIKE en JSONB)."""
        if not is_pg_enabled():
            return []
        domain = _domain(collection_name)
        try:
            rows = await search_chunks_by_title(title_fragment, domain, user_id=user_id)
        except Exception as e:
            logger.error(f"Error en búsqueda por título '{title_fragment}': {e}")
            return []
        return [_map_row(r, distance_override=0.0) for r in rows]

    # ------------------------------------------------------------------ #
    # Eliminación / utilidades                                            #
    # ------------------------------------------------------------------ #
    async def adelete_document(self, doc_id: str, collection_name: str | None = None) -> bool:
        """Elimina todos los chunks de un documento por su doc_id."""
        domain = _domain(collection_name) if collection_name else None
        deleted = await delete_chunks_by_meta("doc_id", doc_id, domain)
        if deleted:
            logger.info(f"Eliminados {deleted} chunks del doc '{doc_id}'")
        return deleted > 0

    async def aclear_collection(self, collection_name: str) -> None:
        """Vacía una colección lógica (dominio de chunks o la caché)."""
        if collection_name == COLLECTION_CACHE:
            n = await clear_semantic_cache_pg()
            logger.info(f"Caché semántica vaciada ({n} entradas)")
            return
        domain = _domain(collection_name)
        n = await clear_chunks_by_domain(domain)
        logger.info(f"Dominio '{domain}' vaciado ({n} chunks)")

    async def ais_document_indexed(
        self, identifier: str, id_type: str = "url", collection_name: str | None = None,
    ) -> bool:
        """Verifica si un documento ya está indexado (por url o doc_id en metadata)."""
        domain = _domain(collection_name) if collection_name else None
        return await chunks_meta_exists(id_type, identifier, domain)

    async def alist_documents(
        self, collection_name: str = COLLECTION_INTERNOS, user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lista documentos únicos (por doc_id) de un dominio."""
        # Solo los documentos internos se listan en la UI.
        return await list_internal_documents(user_id=user_id)

    async def aget_stats(self) -> dict[str, Any]:
        """Estadísticas de conteo por colección lógica."""
        counts = await count_chunks_by_domain()
        normativa = counts.get("normative", 0)
        internos = counts.get("internal", 0)
        return {
            COLLECTION_NORMATIVA: normativa,
            COLLECTION_INTERNOS: internos,
            "total": normativa + internos,
        }

    # ------------------------------------------------------------------ #
    # Caché semántica                                                      #
    # ------------------------------------------------------------------ #
    async def acache_lookup(self, query: str) -> Optional[dict]:
        """Busca una respuesta cacheada semánticamente similar a la query."""
        if not is_pg_enabled():
            return None
        embedding = await self._aembed_one(query)
        if embedding is None:
            return None
        try:
            hit = await cache_lookup_pg(embedding, CACHE_DISTANCE_THRESHOLD, CACHE_TTL_HOURS)
            if hit:
                logger.info("[cache] HIT")
            return hit
        except Exception as e:
            logger.warning(f"[cache] Error en lookup: {e}")
            return None

    async def acache_store(
        self, query: str, answer: str, sources: list, query_type: str,
    ) -> None:
        """Almacena una respuesta en el caché semántico."""
        if not is_pg_enabled():
            return
        embedding = await self._aembed_one(query)
        if embedding is None:
            return
        try:
            await cache_store_pg(query, embedding, answer, sources, query_type)
            logger.info("[cache] Almacenado")
        except Exception as e:
            logger.warning(f"[cache] Error almacenando: {e}")
