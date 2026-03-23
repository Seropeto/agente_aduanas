"""
Módulo de almacenamiento vectorial usando ChromaDB.
Gestiona dos colecciones:
  - normativa_aduanera: documentos scrapeados de fuentes oficiales
  - documentos_internos: documentos subidos por el usuario
"""
import hashlib
import logging
from typing import Any, Optional

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

from backend.config import CHROMA_DIR, EMBEDDING_MODEL

logger = logging.getLogger(__name__)

COLLECTION_NORMATIVA = "normativa_aduanera"
COLLECTION_INTERNOS = "documentos_internos"


class VectorStore:
    """
    Interfaz de alto nivel para ChromaDB.
    Gestiona embeddings, almacenamiento y búsqueda de documentos.
    """

    def __init__(self):
        self._client: Optional[chromadb.PersistentClient] = None
        self._embedding_model: Optional[SentenceTransformer] = None
        self._collection_normativa = None
        self._collection_internos = None
        self._initialized = False

    def initialize(self):
        """Inicializa ChromaDB y el modelo de embeddings."""
        if self._initialized:
            return

        logger.info("Inicializando ChromaDB...")
        try:
            self._client = chromadb.PersistentClient(
                path=str(CHROMA_DIR),
                settings=Settings(anonymized_telemetry=False),
            )

            # Crear/obtener colecciones
            self._collection_normativa = self._client.get_or_create_collection(
                name=COLLECTION_NORMATIVA,
                metadata={
                    "description": "Normativa oficial aduanera chilena",
                    "hnsw:space": "cosine",
                },
            )

            self._collection_internos = self._client.get_or_create_collection(
                name=COLLECTION_INTERNOS,
                metadata={
                    "description": "Documentos internos subidos por el usuario",
                    "hnsw:space": "cosine",
                },
            )

            logger.info("ChromaDB inicializado correctamente")
        except Exception as e:
            logger.error(f"Error inicializando ChromaDB: {e}")
            raise

        logger.info(f"Cargando modelo de embeddings: {EMBEDDING_MODEL}")
        try:
            self._embedding_model = SentenceTransformer(EMBEDDING_MODEL)
            logger.info("Modelo de embeddings cargado")
        except Exception as e:
            logger.error(f"Error cargando modelo de embeddings: {e}")
            raise

        self._initialized = True

    def _ensure_initialized(self):
        """Asegura que el store esté inicializado."""
        if not self._initialized:
            self.initialize()

    def _get_collection(self, collection_name: str):
        """Retorna la colección especificada."""
        if collection_name == COLLECTION_NORMATIVA:
            return self._collection_normativa
        elif collection_name == COLLECTION_INTERNOS:
            return self._collection_internos
        else:
            raise ValueError(f"Colección desconocida: {collection_name}")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Genera embeddings para una lista de textos."""
        try:
            embeddings = self._embedding_model.encode(texts, show_progress_bar=False)
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"Error generando embeddings: {e}")
            raise

    def _generate_chunk_id(self, doc_id: str, chunk_index: int) -> str:
        """Genera un ID único para un chunk."""
        return f"{doc_id}_{chunk_index}"

    # ------------------------------------------------------------------ #
    # Inserción de documentos                                              #
    # ------------------------------------------------------------------ #

    def add_documents(
        self,
        chunks: list[dict[str, Any]],
        collection_name: str = COLLECTION_NORMATIVA,
    ) -> int:
        """
        Agrega chunks de documentos a la colección especificada.

        Args:
            chunks: Lista de dicts con 'text' y 'metadata'.
            collection_name: Nombre de la colección destino.

        Returns:
            Número de chunks insertados exitosamente.
        """
        self._ensure_initialized()
        if not chunks:
            return 0

        collection = self._get_collection(collection_name)
        inserted = 0

        # Procesar en lotes para no saturar la memoria
        batch_size = 50
        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]

            ids = []
            texts = []
            metadatas = []

            for chunk in batch:
                text = chunk.get("text", "").strip()
                meta = chunk.get("metadata", {})

                if not text:
                    continue

                doc_id = meta.get("doc_id", hashlib.sha256(text.encode()).hexdigest()[:16])
                chunk_idx = meta.get("chunk_index", 0)
                chunk_id = self._generate_chunk_id(doc_id, chunk_idx)

                # ChromaDB requiere que los valores de metadata sean str/int/float/bool
                clean_meta = {
                    k: str(v) if not isinstance(v, (str, int, float, bool)) else v
                    for k, v in meta.items()
                    if v is not None
                }
                clean_meta.setdefault("source", "desconocido")
                clean_meta.setdefault("title", "Sin título")
                clean_meta.setdefault("date", "")
                clean_meta.setdefault("url", "")
                clean_meta.setdefault("content_type", "normativa")
                clean_meta.setdefault("doc_id", doc_id)

                ids.append(chunk_id)
                texts.append(text)
                metadatas.append(clean_meta)

            if not ids:
                continue

            try:
                embeddings = self._embed(texts)
                collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas,
                )
                inserted += len(ids)
            except Exception as e:
                logger.error(f"Error insertando batch en {collection_name}: {e}")

        logger.info(f"Insertados {inserted} chunks en '{collection_name}'")
        return inserted

    # ------------------------------------------------------------------ #
    # Búsqueda                                                             #
    # ------------------------------------------------------------------ #

    def search(
        self,
        query: str,
        collection_name: str | None = None,
        top_k: int = 3,
        filter_metadata: dict | None = None,
    ) -> list[dict[str, Any]]:
        """
        Busca documentos relevantes para la consulta dada.

        Args:
            query: Texto de la consulta.
            collection_name: Si es None, busca en ambas colecciones.
            top_k: Número máximo de resultados por colección.
            filter_metadata: Filtros de metadata (ChromaDB where clause).

        Returns:
            Lista de resultados con 'text', 'metadata', 'distance', 'collection'.
        """
        self._ensure_initialized()

        if not query.strip():
            return []

        results = []

        collections_to_search = []
        if collection_name == COLLECTION_NORMATIVA:
            collections_to_search = [COLLECTION_NORMATIVA]
        elif collection_name == COLLECTION_INTERNOS:
            collections_to_search = [COLLECTION_INTERNOS]
        else:
            collections_to_search = [COLLECTION_NORMATIVA, COLLECTION_INTERNOS]

        try:
            query_embedding = self._embed([query])[0]
        except Exception as e:
            logger.error(f"Error generando embedding de consulta: {e}")
            return []

        for coll_name in collections_to_search:
            try:
                collection = self._get_collection(coll_name)

                # Verificar que la colección tenga documentos
                count = collection.count()
                if count == 0:
                    continue

                query_params = {
                    "query_embeddings": [query_embedding],
                    "n_results": min(top_k, count),
                    "include": ["documents", "metadatas", "distances"],
                }
                if filter_metadata:
                    query_params["where"] = filter_metadata

                response = collection.query(**query_params)

                if not response or not response.get("ids"):
                    continue

                ids = response["ids"][0]
                docs = response["documents"][0]
                metas = response["metadatas"][0]
                distances = response["distances"][0]

                for i, doc_id in enumerate(ids):
                    results.append({
                        "text": docs[i],
                        "metadata": metas[i],
                        "distance": distances[i],
                        "collection": coll_name,
                        "relevance_score": 1.0 - distances[i],  # cosine: 0=identical
                    })

            except Exception as e:
                logger.error(f"Error buscando en colección '{coll_name}': {e}")

        # Ordenar por relevancia (menor distancia = más relevante)
        results.sort(key=lambda x: x["distance"])

        return results

    # ------------------------------------------------------------------ #
    # Eliminación                                                          #
    # ------------------------------------------------------------------ #

    def clear_collection(self, collection_name: str) -> None:
        """Elimina y recrea una colección completa (limpieza total)."""
        self._ensure_initialized()
        try:
            self._client.delete_collection(collection_name)
            logger.info(f"Colección '{collection_name}' eliminada")
        except Exception as e:
            logger.warning(f"No se pudo eliminar colección '{collection_name}': {e}")
        # Recrear vacía con los mismos parámetros
        new_col = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        if collection_name == COLLECTION_NORMATIVA:
            self._collection_normativa = new_col
        elif collection_name == COLLECTION_INTERNOS:
            self._collection_internos = new_col
        logger.info(f"Colección '{collection_name}' recreada vacía")

    def delete_document(self, doc_id: str, collection_name: str | None = None) -> bool:
        """
        Elimina todos los chunks de un documento por su doc_id.

        Args:
            doc_id: ID del documento a eliminar.
            collection_name: Colección donde buscar. Si None, busca en ambas.

        Returns:
            True si se eliminó al menos un chunk.
        """
        self._ensure_initialized()

        collections_to_check = []
        if collection_name:
            collections_to_check = [collection_name]
        else:
            collections_to_check = [COLLECTION_NORMATIVA, COLLECTION_INTERNOS]

        deleted_any = False

        for coll_name in collections_to_check:
            try:
                collection = self._get_collection(coll_name)

                # Obtener IDs de chunks que corresponden al doc_id
                results = collection.get(
                    where={"doc_id": doc_id},
                    include=["metadatas"],
                )

                if results and results.get("ids"):
                    chunk_ids = results["ids"]
                    collection.delete(ids=chunk_ids)
                    logger.info(
                        f"Eliminados {len(chunk_ids)} chunks del doc '{doc_id}' en '{coll_name}'"
                    )
                    deleted_any = True
            except Exception as e:
                logger.error(f"Error eliminando doc '{doc_id}' de '{coll_name}': {e}")

        return deleted_any

    # ------------------------------------------------------------------ #
    # Estadísticas y utilidades                                           #
    # ------------------------------------------------------------------ #

    def get_stats(self) -> dict[str, Any]:
        """Retorna estadísticas de las colecciones."""
        self._ensure_initialized()
        try:
            return {
                COLLECTION_NORMATIVA: self._collection_normativa.count(),
                COLLECTION_INTERNOS: self._collection_internos.count(),
                "total": (
                    self._collection_normativa.count()
                    + self._collection_internos.count()
                ),
            }
        except Exception as e:
            logger.error(f"Error obteniendo estadísticas: {e}")
            return {COLLECTION_NORMATIVA: 0, COLLECTION_INTERNOS: 0, "total": 0}

    def is_document_indexed(
        self,
        identifier: str,
        id_type: str = "url",
        collection_name: str | None = None,
    ) -> bool:
        """
        Verifica si un documento ya está indexado.

        Args:
            identifier: URL o hash del archivo a verificar.
            id_type: 'url' para buscar por URL, 'doc_id' para buscar por hash.
            collection_name: Colección donde buscar. Si None, busca en ambas.

        Returns:
            True si el documento ya está indexado.
        """
        self._ensure_initialized()

        collections_to_check = []
        if collection_name:
            collections_to_check = [collection_name]
        else:
            collections_to_check = [COLLECTION_NORMATIVA, COLLECTION_INTERNOS]

        for coll_name in collections_to_check:
            try:
                collection = self._get_collection(coll_name)
                if collection.count() == 0:
                    continue

                where_clause = {id_type: identifier}
                results = collection.get(
                    where=where_clause,
                    limit=1,
                    include=["metadatas"],
                )

                if results and results.get("ids"):
                    return True
            except Exception as e:
                logger.debug(f"Error verificando existencia en '{coll_name}': {e}")

        return False

    def list_documents(self, collection_name: str = COLLECTION_INTERNOS) -> list[dict[str, Any]]:
        """
        Lista los documentos únicos (no chunks) en una colección.

        Args:
            collection_name: Nombre de la colección.

        Returns:
            Lista de documentos con metadata única por doc_id.
        """
        self._ensure_initialized()

        try:
            collection = self._get_collection(collection_name)
            count = collection.count()
            if count == 0:
                return []

            # Obtener todos los chunks con sus metadatas
            results = collection.get(
                limit=min(count, 10000),
                include=["metadatas"],
            )

            if not results or not results.get("metadatas"):
                return []

            # Agrupar por doc_id para mostrar un documento único
            seen_doc_ids = {}
            for meta in results["metadatas"]:
                doc_id = meta.get("doc_id", "")
                if doc_id and doc_id not in seen_doc_ids:
                    seen_doc_ids[doc_id] = {
                        "doc_id": doc_id,
                        "title": meta.get("title", "Sin título"),
                        "filename": meta.get("filename", ""),
                        "date": meta.get("date", ""),
                        "url": meta.get("url", ""),
                        "content_type": meta.get("content_type", ""),
                        "source": meta.get("source", ""),
                        "total_chunks": int(meta.get("total_chunks", 1)),
                    }

            return list(seen_doc_ids.values())

        except Exception as e:
            logger.error(f"Error listando documentos de '{collection_name}': {e}")
            return []

    def close(self):
        """Cierra la conexión con ChromaDB."""
        # ChromaDB PersistentClient no requiere cierre explícito
        logger.info("VectorStore cerrado")
