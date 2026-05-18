"""
Motor RAG (Retrieval-Augmented Generation) para el Agente de Aduanas Chile.
Integra ChromaDB para recuperación de documentos y Claude para generación de respuestas.
"""
import asyncio
import logging
import re
from typing import Any, Optional

import json

import anthropic

from backend.config import ANTHROPIC_API_KEY, MODEL_NAME, TOP_K_RESULTS
from backend.memory import (
    get_history, save_turn,
    needs_summary_update, get_turns_to_summarize, save_summary,
)
from backend.normative_changelog import get_changes_by_period, parse_temporal_query

MODEL_SIMPLE  = "claude-haiku-4-5-20251001"   # consultas simples (~70% más barato)
MODEL_COMPLEX = "claude-haiku-4-5-20251001"   # TEMPORAL: Sonnet 4.6 con incidente activo (revertir cuando Anthropic resuelva)

SIMPLE_KEYWORDS = [
    "qué es", "que es", "qué son", "que son", "define", "definición",
    "cuándo", "cuando", "cuánto", "cuanto", "plazo", "fecha",
    "cuál es el", "cual es el", "dónde", "donde",
    "lista", "listado", "enumera", "menciona",
    "significa", "concepto", "nombre",
]

COMPLEX_KEYWORDS = [
    "analiza", "análisis", "compara", "comparación", "diferencia",
    "implica", "implicancia", "interpreta", "interpretación",
    "cómo afecta", "como afecta", "impacto", "consecuencia",
    "explica en detalle", "procedimiento completo", "caso práctico",
    "cuál conviene", "recomienda", "estrategia",
]

# Umbral máximo de distancia (0=idéntico, 1=nada que ver).
# Los documentos internos solo se incluyen en modo "all" si son genuinamente
# relevantes para la pregunta. Esto evita que contratos o documentos operativos
# aparezcan en consultas de normativa general.
INTERNAL_MAX_DISTANCE = 0.40   # ≥ 60 % de similitud semántica requerida
NORMATIVA_MAX_DISTANCE = 0.50  # ≥ 50 % de similitud — balance entre relevancia y cobertura
from backend.indexer.vectorstore import (
    VectorStore,
    COLLECTION_NORMATIVA,
    COLLECTION_INTERNOS,
)

logger = logging.getLogger(__name__)

# Tipos de consulta para ajustar la búsqueda
QUERY_TYPES = {
    "arancelaria": [
        "arancel", "clasificación", "partida", "subpartida", "ncm", "sa ",
        "código arancelario", "gravamen", "derechos ad valorem", "tasa",
        "capítulo", "fracción arancelaria",
    ],
    "tramite": [
        "trámite", "tramite", "procedimiento", "formulario", "declaración",
        "dua", "dam", "importar", "exportar", "despacho", "aforo",
        "canal", "plazos", "requisitos", "cómo", "como ", "pasos",
    ],
    "normativa": [
        "circular", "resolución", "resolucion", "decreto", "ley", "ordenanza",
        "artículo", "articulo", "norma", "reglamento", "vigente", "modificación",
        "publicó", "publico", "diario oficial",
    ],
    "general": [],  # Tipo por defecto
}

SYSTEM_PROMPT = """Eres un experto en aduanas y comercio exterior de Chile. Dominas la Ordenanza de Aduanas (DFL N°30/2005), el Arancel Aduanero, Circulares y Resoluciones de Aduanas, procedimientos de importación/exportación, IVA e impuestos aduaneros, acuerdos de libre comercio, zonas francas y normativa del SII relacionada con importaciones.

Los documentos que recibes en el contexto son REALES y ACTUALIZADOS, descargados automáticamente desde aduana.cl, leychile.cl y el Diario Oficial. Úsalos como fuente principal y cítalos con precisión (nombre, número, fecha).

Reglas absolutas:
- Responde siempre en español técnico aduanero chileno.
- NUNCA menciones "corte de entrenamiento", "fecha de corte" ni limitaciones de la IA. Responde como el experto que eres.
- Si no hay documentos en contexto: responde con tu expertise y orienta a https://www.aduana.cl o https://www.diariooficial.interior.gob.cl para verificar publicaciones recientes.
- Cita circulares y resoluciones con su número exacto. Estructura en listas cuando aplique.
- Al usar fuentes del contexto, termina con: **Fuentes consultadas:** [lista]"""


class RAGEngine:
    """
    Motor RAG que combina búsqueda vectorial con generación de Claude.
    """

    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store
        self._anthropic_client: Optional[anthropic.Anthropic] = None
        self._async_anthropic_client: Optional[anthropic.AsyncAnthropic] = None

    def _get_anthropic_client(self) -> anthropic.Anthropic:
        """Retorna el cliente sincrónico de Anthropic."""
        if not self._anthropic_client:
            if not ANTHROPIC_API_KEY:
                raise ValueError(
                    "ANTHROPIC_API_KEY no configurada. "
                    "Agregue su clave API en el archivo .env"
                )
            self._anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return self._anthropic_client

    def _get_async_anthropic_client(self) -> anthropic.AsyncAnthropic:
        """Retorna el cliente asíncrono de Anthropic (para streaming)."""
        if not self._async_anthropic_client:
            if not ANTHROPIC_API_KEY:
                raise ValueError(
                    "ANTHROPIC_API_KEY no configurada. "
                    "Agregue su clave API en el archivo .env"
                )
            self._async_anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        return self._async_anthropic_client

    def classify_query(self, query: str) -> str:
        """
        Clasifica el tipo de consulta para ajustar la estrategia de búsqueda.

        Returns:
            'arancelaria', 'tramite', 'normativa', o 'general'
        """
        query_lower = query.lower()

        scores = {}
        for query_type, keywords in QUERY_TYPES.items():
            if query_type == "general":
                continue
            score = sum(1 for kw in keywords if kw in query_lower)
            scores[query_type] = score

        if not scores or max(scores.values()) == 0:
            return "general"

        return max(scores, key=scores.get)

    def classify_complexity(self, query: str) -> str:
        """
        Determina si la consulta es simple o compleja para seleccionar el modelo.
        Retorna 'simple' o 'complex'.
        """
        q = query.lower()
        if any(kw in q for kw in COMPLEX_KEYWORDS):
            return "complex"
        if any(kw in q for kw in SIMPLE_KEYWORDS):
            return "simple"
        # Por defecto, consultas cortas son simples
        return "simple" if len(query.split()) <= 8 else "complex"

    def _select_model(self, query: str) -> str:
        """Selecciona el modelo según la complejidad de la consulta."""
        complexity = self.classify_complexity(query)
        model = MODEL_SIMPLE if complexity == "simple" else MODEL_COMPLEX
        logger.info(f"Modelo seleccionado: {model} (complejidad: {complexity})")
        return model

    def _determine_collection_priority(
        self,
        query_type: str,
        filter_override: str = "all",
    ) -> list[tuple[str, int]]:
        """
        Determina el orden de prioridad de las colecciones y cuántos resultados
        obtener de cada una.

        Returns:
            Lista de (collection_name, top_k) en orden de prioridad.
        """
        if filter_override == "normativa":
            return [(COLLECTION_NORMATIVA, TOP_K_RESULTS)]
        elif filter_override == "internos":
            return [(COLLECTION_INTERNOS, TOP_K_RESULTS)]

        # Prioridad según tipo de consulta
        if query_type in ("arancelaria", "normativa"):
            return [
                (COLLECTION_NORMATIVA, TOP_K_RESULTS),
                (COLLECTION_INTERNOS, TOP_K_RESULTS),
            ]
        elif query_type == "tramite":
            return [
                (COLLECTION_INTERNOS, TOP_K_RESULTS),
                (COLLECTION_NORMATIVA, TOP_K_RESULTS),
            ]
        else:
            return [
                (COLLECTION_NORMATIVA, TOP_K_RESULTS),
                (COLLECTION_INTERNOS, TOP_K_RESULTS),
            ]

    def _build_context(self, search_results: list[dict[str, Any]]) -> tuple[str, list[dict]]:
        """
        Construye el contexto textual y la lista de fuentes a partir de los resultados de búsqueda.

        Returns:
            (context_text, sources_list)
        """
        if not search_results:
            return "", []

        context_parts = []
        sources = []
        seen_doc_ids = set()

        for i, result in enumerate(search_results, 1):
            text = result.get("text", "")
            meta = result.get("metadata", {})
            collection = result.get("collection", "")
            relevance = result.get("relevance_score", 0.0)

            if not text:
                continue

            # Metadata del documento
            title = meta.get("title", "Documento sin título")
            source = meta.get("source", "Fuente desconocida")
            url = meta.get("url", "")
            date = meta.get("date", "")
            content_type = meta.get("content_type", "")
            doc_id = meta.get("doc_id", "")

            # Etiqueta de la fuente para el contexto
            source_label = self._format_source_label(title, source, content_type, date)

            # Sección del contexto
            context_part = f"[FUENTE {i}: {source_label}]\n{text}\n"
            context_parts.append(context_part)

            # Agregar a fuentes únicas
            if doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                source_entry = {
                    "title": title,
                    "source": source,
                    "url": url,
                    "date": date,
                    "content_type": content_type,
                    "collection": "normativa" if collection == COLLECTION_NORMATIVA else "interno",
                    "relevance": round(relevance, 3),
                }
                sources.append(source_entry)

        context_text = "\n---\n".join(context_parts)
        return context_text, sources

    def _format_source_label(
        self,
        title: str,
        source: str,
        content_type: str,
        date: str,
    ) -> str:
        """Formatea una etiqueta descriptiva para la fuente."""
        type_labels = {
            "circular": "Circular",
            "resolucion": "Resolución",
            "arancel": "Arancel",
            "procedimiento": "Procedimiento",
            "ley": "Ley",
            "decreto": "Decreto",
            "normativa": "Normativa",
            "publicacion": "Publicación",
        }
        type_str = type_labels.get(content_type, content_type.title() if content_type else "Documento")
        date_str = f" ({date})" if date else ""
        return f"{type_str}: {title}{date_str} — {source}"

    def _build_user_message(self, query: str, context: str, query_type: str) -> str:
        """Construye el mensaje del usuario para Claude."""
        type_hints = {
            "arancelaria": "Esta es una consulta sobre clasificación arancelaria o tarifas.",
            "tramite": "Esta es una consulta sobre procedimientos o trámites aduaneros.",
            "normativa": "Esta es una consulta sobre normativa, circulares o resoluciones aduaneras.",
            "general": "",
        }
        hint = type_hints.get(query_type, "")

        if context:
            message = f"""Documentos oficiales disponibles en la base de datos:

{context}

---

{hint}
Consulta del usuario: {query}

Instrucciones:
- Usa los documentos anteriores como fuente principal y cítalos con precisión (nombre, número, fecha).
- Si los documentos no contienen información suficiente, complementa con tu expertise en normativa aduanera chilena, pero sin mencionar limitaciones técnicas del sistema."""
        else:
            message = f"""{hint}
Consulta del usuario: {query}

No hay documentos específicos sobre este tema en la base de datos en este momento.

Responde como experto en normativa aduanera chilena. Reglas:
1. NUNCA menciones "corte de entrenamiento", "fecha de corte", "conocimiento hasta X año" ni nada similar.
2. Da una respuesta concreta y útil basada en tu expertise.
3. Si preguntan por cambios "de la semana pasada" o "recientes": entrega los cambios normativos más relevantes y recientes que conozcas, y al final indica: "Para verificar las publicaciones más recientes, revisa directamente https://www.aduana.cl y https://www.diariooficial.interior.gob.cl"
4. Nunca respondas solo con una negativa."""

        return message

    def _build_changelog_context(self, changes: list[dict], period_desc: str) -> str:
        """Construye el contexto textual a partir de entradas del changelog."""
        if not changes:
            return f"No se encontraron cambios normativos para el período: {period_desc}."

        lines = [f"CAMBIOS NORMATIVOS — {period_desc.upper()}\n"]
        type_labels = {
            "incorporacion": "NUEVA NORMA",
            "modificacion":  "MODIFICACIÓN",
            "derogacion":    "DEROGACIÓN",
        }
        for ch in changes:
            tipo  = type_labels.get(ch.get("change_type", ""), "CAMBIO")
            title = ch.get("title", "Sin título")
            fecha = ch.get("change_date", "")
            url   = ch.get("source_url", "")
            summ  = ch.get("summary", "")

            lines.append(f"[{tipo}] {title}")
            if fecha:
                lines.append(f"  Fecha: {fecha}")
            if url:
                lines.append(f"  Fuente: {url}")
            if summ:
                lines.append(f"  Resumen: {summ}")
            lines.append("")

        return "\n".join(lines)

    async def _answer_temporal_query(
        self,
        query: str,
        changes: list[dict],
        period_desc: str,
        history: list[dict],
    ) -> str:
        """Llama a Claude con el contexto del changelog para responder la consulta temporal."""
        context = self._build_changelog_context(changes, period_desc)

        if not changes:
            user_msg = (
                f"El usuario pregunta: {query}\n\n"
                f"No hay registros de cambios normativos para '{period_desc}' en la base de datos. "
                "Informa al usuario y sugiere revisar directamente www.aduana.cl y "
                "www.diariooficial.interior.gob.cl para obtener información actualizada."
            )
        else:
            user_msg = (
                f"Datos de cambios normativos aduaneros para el período '{period_desc}':\n\n"
                f"{context}\n\n"
                f"Consulta del usuario: {query}\n\n"
                "Responde describiendo los cambios encontrados en lenguaje técnico aduanero. "
                "Organiza por tipo: nuevas normas primero, luego modificaciones y derogaciones. "
                "Cita título y fecha de cada cambio. "
                "Si hay pocas entradas, indica que el registro se va completando "
                "a medida que el sistema detecta nuevas publicaciones."
            )

        messages = [*history, {"role": "user", "content": user_msg}]
        try:
            client   = self._get_async_anthropic_client()
            response = await client.messages.create(
                model=MODEL_SIMPLE,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            return response.content[0].text
        except Exception as e:
            logger.warning(f"Error generando respuesta temporal: {e}")
            return context  # fallback: devolver el contexto crudo

    async def query(
        self,
        query: str,
        filter_collection: str = "all",
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Procesa una consulta RAG: recupera contexto y genera respuesta con Claude.

        Args:
            query: Pregunta del usuario en español.
            filter_collection: 'all', 'normativa', o 'internos'.

        Returns:
            {
                'answer': str,
                'sources': list[dict],
                'query_type': str,
                'total_chunks_retrieved': int,
            }
        """
        if not query or not query.strip():
            return {
                "answer": "Por favor, ingrese una consulta válida.",
                "sources": [],
                "query_type": "general",
                "total_chunks_retrieved": 0,
            }

        # 0. Detección de consultas temporales (REQ-10)
        temporal = await asyncio.to_thread(parse_temporal_query, query)
        if temporal:
            from_d, to_d, period_desc = temporal
            history = await asyncio.to_thread(get_history, user_id) if user_id else []
            changes = await asyncio.to_thread(get_changes_by_period, from_d, to_d)
            answer  = await self._answer_temporal_query(query, changes, period_desc, history)
            sources = [
                {"title": ch["title"], "source": ch.get("source_url", ""),
                 "date": ch["change_date"], "content_type": ch["change_type"],
                 "url": ch.get("source_url", ""), "collection": "changelog"}
                for ch in changes
            ]
            if user_id and answer:
                try:
                    save_turn(user_id, session_id or "default", query, answer, sources)
                    asyncio.create_task(self._maybe_update_summary(user_id))
                except Exception as e:
                    logger.warning(f"Error guardando turno temporal: {e}")
            return {
                "answer": answer,
                "sources": sources,
                "query_type": "normativa",
                "total_chunks_retrieved": len(changes),
            }

        # 1. Clasificar la consulta (CPU-only, sin IO)
        query_type = self.classify_query(query)
        logger.info(f"Consulta clasificada como: {query_type}")

        # 2. Paralelo: caché + historial + recuperación documental (REQ-09)
        async def _cache():
            if filter_collection != "all":
                return None
            try:
                return await asyncio.to_thread(self.vector_store.cache_lookup, query)
            except Exception as e:
                logger.warning(f"Error en caché lookup: {e}")
                return None

        async def _history():
            if not user_id:
                return []
            try:
                return await asyncio.to_thread(get_history, user_id)
            except Exception as e:
                logger.warning(f"Error recuperando historial: {e}")
                return []

        cached, history, (top_results, context_text, sources) = await asyncio.gather(
            _cache(),
            _history(),
            asyncio.to_thread(self._retrieve_documents, query, query_type, filter_collection, user_id),
        )

        if cached:
            return {
                "answer": cached["answer"],
                "sources": cached["sources"],
                "query_type": cached["query_type"],
                "total_chunks_retrieved": 0,
                "cache_hit": True,
            }

        # 3. Construir mensajes para Claude
        user_message = self._build_user_message(query, context_text, query_type)
        messages = [*history, {"role": "user", "content": user_message}]

        # 4. Llamar a Claude (cliente async)
        answer = ""
        try:
            client = self._get_async_anthropic_client()
            response = await client.messages.create(
                model=self._select_model(query),
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            answer = response.content[0].text
        except anthropic.AuthenticationError:
            logger.error("Error de autenticación con la API de Anthropic")
            answer = (
                "Error: La clave API de Anthropic no es válida o no está configurada. "
                "Por favor, verifique el archivo .env con su ANTHROPIC_API_KEY."
            )
        except anthropic.RateLimitError:
            logger.warning("Límite de tasa de la API de Anthropic alcanzado")
            answer = (
                "El servicio está temporalmente sobrecargado. "
                "Por favor, intente nuevamente en unos momentos."
            )
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", 0)
            body = getattr(e, "body", None) or {}
            err_type = body.get("error", {}).get("type", "") if isinstance(body, dict) else ""
            logger.warning(f"APIStatusError {status} ({err_type}): {e}")
            if err_type == "overloaded_error" or status == 529:
                answer = "Los servidores de IA están momentáneamente saturados. Por favor, intente nuevamente en unos segundos."
            else:
                answer = "Error del servicio de IA. Por favor, intente nuevamente."
        except Exception as e:
            logger.error(f"Error llamando a la API de Anthropic: {e}")
            answer = "Ocurrió un error al procesar su consulta. Por favor, intente nuevamente."

        # 6. Persistir turno en memoria y guardar en caché
        if user_id and answer and not answer.startswith("Error:"):
            try:
                save_turn(user_id, session_id or "default", query, answer, sources)
                asyncio.create_task(self._maybe_update_summary(user_id))
            except Exception as e:
                logger.warning(f"Error guardando turno en memoria: {e}")

        if answer and not answer.startswith("Error:") and filter_collection == "all":
            try:
                self.vector_store.cache_store(query, answer, sources, query_type)
            except Exception as e:
                logger.warning(f"Error guardando en caché: {e}")

        return {
            "answer": answer,
            "sources": sources,
            "query_type": query_type,
            "total_chunks_retrieved": len(top_results),
        }

    async def _maybe_update_summary(self, user_id: str) -> None:
        """
        Genera y cachea en SQLite el resumen de turnos antiguos usando Haiku.
        Se ejecuta en background — el usuario ya tiene su respuesta.
        """
        try:
            if not await asyncio.to_thread(needs_summary_update, user_id):
                return
            turns, last_id = await asyncio.to_thread(get_turns_to_summarize, user_id)
            if not turns or last_id is None:
                return

            conv_text = "\n".join(
                f"{'Usuario' if t['role'] == 'user' else 'Agente'}: {t['content'][:600]}"
                for t in turns
            )

            client = self._get_async_anthropic_client()
            response = await client.messages.create(
                model=MODEL_SIMPLE,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": (
                        "Resume la siguiente conversación sobre normativa aduanera chilena "
                        "en máximo 150 palabras. Conserva: temas tratados, normas o circulares "
                        "citadas, fechas relevantes y conclusiones clave. Omite saludos y "
                        "preguntas genéricas.\n\n"
                        f"CONVERSACIÓN:\n{conv_text}\n\nRESUMEN:"
                    ),
                }],
            )
            summary = response.content[0].text.strip()
            await asyncio.to_thread(save_summary, user_id, summary, last_id)
        except Exception as e:
            logger.warning(f"Error generando resumen de historial: {e}")

    def _retrieve_documents(
        self,
        query: str,
        query_type: str,
        filter_collection: str,
        user_id: str | None,
    ) -> tuple[list[dict], str, list[dict]]:
        """
        Recupera documentos del vector store, filtra y construye contexto.
        Retorna (top_results, context_text, sources).
        """
        collection_strategy = self._determine_collection_priority(query_type, filter_collection)

        all_results = []
        for collection_name, top_k in collection_strategy:
            try:
                meta_filter = None
                if collection_name == COLLECTION_INTERNOS and user_id:
                    meta_filter = {"user_id": user_id}
                results = self.vector_store.search(
                    query=query,
                    collection_name=collection_name,
                    top_k=top_k,
                    filter_metadata=meta_filter,
                )
                all_results.extend(results)
            except Exception as e:
                logger.error(f"Error buscando en {collection_name}: {e}")

        if filter_collection == "all":
            filtered = []
            for r in all_results:
                d = r.get("distance", 1.0)
                if r.get("collection") == COLLECTION_INTERNOS:
                    if d <= INTERNAL_MAX_DISTANCE:
                        filtered.append(r)
                else:
                    if d <= NORMATIVA_MAX_DISTANCE:
                        filtered.append(r)
            all_results = filtered

        all_results.sort(key=lambda x: x.get("distance", 1.0))
        top_results = all_results[:6]
        context_text, sources = self._build_context(top_results)
        return top_results, context_text, sources

    async def query_stream(
        self,
        query: str,
        filter_collection: str = "all",
        user_id: str | None = None,
        session_id: str | None = None,
    ):
        """
        Versión streaming de query(). Genera eventos SSE:
          {"type": "stage",  "text": "..."}   — etapa del proceso
          {"type": "token",  "text": "..."}   — fragmento de respuesta
          {"type": "done",   "sources": [...], "query_type": "...", "chunks": N}
          {"type": "error",  "text": "..."}   — error fatal
        """
        def _sse(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if not query or not query.strip():
            yield _sse({"type": "error", "text": "Por favor, ingrese una consulta válida."})
            return

        # 0. Detección de consultas temporales (REQ-10)
        temporal = await asyncio.to_thread(parse_temporal_query, query)
        if temporal:
            from_d, to_d, period_desc = temporal
            yield _sse({"type": "stage", "text": f"Consultando historial normativo ({period_desc})..."})
            history = await asyncio.to_thread(get_history, user_id) if user_id else []
            changes = await asyncio.to_thread(get_changes_by_period, from_d, to_d)
            answer  = await self._answer_temporal_query(query, changes, period_desc, history)
            sources = [
                {"title": ch["title"], "source": ch.get("source_url", ""),
                 "date": ch["change_date"], "content_type": ch["change_type"],
                 "url": ch.get("source_url", ""), "collection": "changelog"}
                for ch in changes
            ]
            yield _sse({"type": "token", "text": answer})
            if user_id and answer:
                try:
                    save_turn(user_id, session_id or "default", query, answer, sources)
                    asyncio.create_task(self._maybe_update_summary(user_id))
                except Exception as e:
                    logger.warning(f"[stream] Error guardando turno temporal: {e}")
            yield _sse({
                "type": "done",
                "sources": sources,
                "query_type": "normativa",
                "chunks": len(changes),
            })
            return

        # 1. Clasificar la consulta (CPU-only)
        query_type = self.classify_query(query)
        logger.info(f"[stream] Consulta clasificada como: {query_type}")

        # 2. Paralelo: caché + historial + recuperación documental (REQ-09)
        yield _sse({"type": "stage", "text": "Consultando base normativa..."})

        async def _cache():
            if filter_collection != "all":
                return None
            try:
                return await asyncio.to_thread(self.vector_store.cache_lookup, query)
            except Exception as e:
                logger.warning(f"[stream] Error en caché lookup: {e}")
                return None

        async def _history():
            if not user_id:
                return []
            try:
                return await asyncio.to_thread(get_history, user_id)
            except Exception as e:
                logger.warning(f"[stream] Error recuperando historial: {e}")
                return []

        cached, history, (top_results, context_text, sources) = await asyncio.gather(
            _cache(),
            _history(),
            asyncio.to_thread(self._retrieve_documents, query, query_type, filter_collection, user_id),
        )

        if cached:
            logger.info("[stream] Cache hit — devolviendo respuesta cacheada")
            yield _sse({"type": "stage", "text": "Respuesta encontrada en caché..."})
            yield _sse({"type": "token", "text": cached["answer"]})
            yield _sse({
                "type": "done",
                "sources": cached["sources"],
                "query_type": cached["query_type"],
                "chunks": 0,
                "cache_hit": True,
            })
            return

        yield _sse({"type": "stage", "text": "Generando respuesta..."})

        user_message = self._build_user_message(query, context_text, query_type)
        messages = [*history, {"role": "user", "content": user_message}]

        full_answer = ""
        try:
            client = self._get_async_anthropic_client()
            async with client.messages.stream(
                model=self._select_model(query),
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    full_answer += text
                    yield _sse({"type": "token", "text": text})

        except anthropic.AuthenticationError:
            logger.error("[stream] Error de autenticación con Anthropic")
            yield _sse({"type": "error", "text": "Error de autenticación con la API. Verifique ANTHROPIC_API_KEY."})
            return
        except anthropic.RateLimitError:
            logger.warning("[stream] Rate limit de Anthropic alcanzado")
            yield _sse({"type": "error", "text": "Servicio temporalmente sobrecargado. Intente en unos momentos."})
            return
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", 0)
            body = getattr(e, "body", None) or {}
            err_type = body.get("error", {}).get("type", "") if isinstance(body, dict) else ""
            logger.warning(f"[stream] APIStatusError {status} ({err_type}): {e}")
            if err_type == "overloaded_error" or status == 529:
                yield _sse({"type": "error", "text": "Los servidores de IA están momentáneamente saturados. Intente nuevamente en unos segundos."})
            else:
                yield _sse({"type": "error", "text": "Error del servicio de IA. Intente nuevamente."})
            return
        except Exception as e:
            logger.error(f"[stream] {type(e).__name__}: {e}")
            yield _sse({"type": "error", "text": "Ocurrió un error al procesar su consulta. Intente nuevamente."})
            return

        # Persistir turno en memoria y guardar en caché
        if user_id and full_answer:
            try:
                save_turn(user_id, session_id or "default", query, full_answer, sources)
                asyncio.create_task(self._maybe_update_summary(user_id))
            except Exception as e:
                logger.warning(f"[stream] Error guardando turno en memoria: {e}")

        if full_answer and filter_collection == "all":
            try:
                self.vector_store.cache_store(query, full_answer, sources, query_type)
            except Exception as e:
                logger.warning(f"[stream] Error guardando en caché: {e}")

        yield _sse({
            "type": "done",
            "sources": sources,
            "query_type": query_type,
            "chunks": len(top_results),
        })
