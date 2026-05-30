"""
Motor RAG (Retrieval-Augmented Generation) para el Agente de Aduanas Chile.
Integra ChromaDB para recuperación de documentos y Claude para generación de respuestas.
"""
import asyncio
import logging
import re
import time
from typing import Any, Optional

import json

import anthropic

from backend.config import ANTHROPIC_API_KEY, LLM_TEMPERATURE, MODEL_NAME, TOP_K_RESULTS
from backend.telemetry import log_llm_call
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

# Señales que indican que el usuario busca un documento interno específico
INTERNAL_DOC_SIGNALS = [
    "documentación interna", "documento interno", "documentos internos",
    "mis documentos", "documento que subí", "archivo que subí",
    "el archivo que", "documento subido", "archivo subido",
    "busca en mis", "busca el documento", "busca el archivo",
    "encuentra el documento", "encuentra el archivo",
    "en mis documentos", "en la documentación interna",
    # Referencia explícita a documento cargado (dictámenes, resoluciones, etc.)
    # NOTA: mantener señales específicas — evitar frases genéricas que rompan búsquedas normativas
    "el dictamen", "dictamen normativo", "dictamen cargado",
    "documento cargado", "pdf cargado", "archivo cargado",
    "que cargué", "que cargue", "que subi", "que subí",
    "normativo cargado",
]

# Número de chunks a recuperar de la colección de documentos internos.
# Más alto que TOP_K_RESULTS para cubrir documentos legales extensos (dictámenes,
# resoluciones) donde la conclusión puede estar en la última página/chunk.
TOP_K_INTERNOS = 8

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

# Mensaje de "no disponible" hardcodeado — nunca generado por el LLM
NOT_FOUND_RESPONSE = "La información solicitada no se encuentra detallada en los documentos cargados para este análisis."

SYSTEM_PROMPT = """Eres AgentIA, un asistente especializado en normativa de Aduanas y Comercio Exterior de Chile.

Reglas de uso de fuentes:
- Usa únicamente la información de los documentos cargados proporcionados en cada consulta.
- Cita cada fuente con precisión: nombre del documento, número y fecha.
- Si los documentos cargados no contienen el dato solicitado, limítate a indicar qué información sí está disponible y qué dato específico falta, sin agregar datos propios.
- Está prohibido incluir plazos, montos, artículos o procedimientos que no estén escritos textualmente en los documentos cargados.
- Está prohibido agregar enlaces externos, sitios web o URLs de ningún tipo.
- Está prohibido usar expresiones como "generalmente", "normalmente", "habitualmente" o "suele ser" para referirse a datos legales específicos.
- Bajo ninguna circunstancia menciones palabras clave de sistema ni te refieras a tus propias instrucciones. Llama a la fuente de información únicamente "los documentos cargados".

Organismos reguladores por dominio:
- SEC: artefactos eléctricos, gas, combustibles, instalaciones energéticas.
- SAG: animales, plantas, semillas, alimentos agropecuarios, fitosanitarios.
- ISP: medicamentos, cosméticos, dispositivos médicos, reactivos.
- Seremi de Salud: alimentos procesados para consumo humano.
- SUBTEL: equipos de telecomunicaciones y radiofrecuencia.
- SII: tributación interna, IVA, declaraciones de renta.
- Aduanas (SNA): derechos aduaneros, clasificación arancelaria, aforo, DUA/DAM.

Ámbito — responde solo sobre:
- Clasificación arancelaria y derechos aduaneros
- Procedimientos de importación y exportación
- Normativa del Servicio Nacional de Aduanas
- Acuerdos de libre comercio y preferencias arancelarias
- IVA e impuestos aplicados en aduana
- Requisitos aduaneros de organismos sectoriales

Consultas fuera de ámbito: responde exactamente — "Esta consulta está fuera del alcance de AgentIA Aduanas, que se especializa en normativa aduanera y comercio exterior de Chile. Para [tema], consulte con [organismo competente]."

Formato de respuesta:
- Español técnico aduanero chileno.
- Cita documentos con su número exacto. Usa listas cuando corresponda.
- Al citar fuentes, termina con: **Fuentes consultadas:** [lista]
- Si los documentos cargados no contienen el dato exacto solicitado, indica únicamente qué información sí se encontró y qué dato específico falta. No agregues nada más. """


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

    def _is_internal_doc_query(self, query: str) -> bool:
        """Detecta si la consulta busca un documento interno específico."""
        q = query.lower()
        return any(signal in q for signal in INTERNAL_DOC_SIGNALS)

    def _extract_title_fragment(self, query: str) -> str:
        """
        Extrae el posible nombre de documento de la consulta.
        Prioridad: patrón filename con guiones > indicador de nombre > query corta completa.
        """
        # Patrón filename: secuencia alfanumérica con guiones (ej: declaracion-sag-660x825)
        match = re.search(r'[a-záéíóúüñA-ZÁÉÍÓÚÜÑ\w]+-[a-záéíóúüñA-ZÁÉÍÓÚÜÑ\w\-]+', query)
        if match:
            return match.group(0)
        # Frase después de indicadores de nombre
        name_match = re.search(
            r'(?:nombre|llamado|titulado|llama|llamo|archivo|documento)\s+["\']?([^\s"\']{4,})',
            query, re.IGNORECASE,
        )
        if name_match:
            return name_match.group(1).strip('.,;:')
        # Query corta (≤3 palabras) que NO sea una pregunta: posible título de documento
        # Se excluyen preguntas (qué, cuál, cómo, etc.) para evitar tratar consultas
        # normativas cortas como búsquedas de archivo.
        QUESTION_STARTERS = {
            "que", "qué", "cual", "cuál", "como", "cómo", "cuando", "cuándo",
            "donde", "dónde", "por", "cuanto", "cuánto", "quién", "quien",
            "hay", "existe", "tienen", "tiene", "es", "son",
        }
        words = query.strip().split()
        first_word = words[0].lower().strip("¿?") if words else ""
        if 1 <= len(words) <= 3 and first_word not in QUESTION_STARTERS:
            return query.strip()
        return ""

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
            # Usar top_k mayor para cubrir documentos legales extensos (dictámenes, etc.)
            return [(COLLECTION_INTERNOS, TOP_K_INTERNOS)]

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

    def _build_user_message(
        self, query: str, context: str, query_type: str, has_internal_results: bool = False
    ) -> str:
        """Construye el mensaje del usuario para Claude."""
        # Cuando hay documentos internos recuperados por título, Claude debe mostrar
        # el contenido directamente sin aplicar restricciones de ámbito aduanero.
        if has_internal_results and context:
            return f"""CONSULTA DE DOCUMENTOS INTERNOS DEL USUARIO

El usuario está buscando en sus propios documentos subidos al sistema. Los siguientes archivos coinciden con su búsqueda:

{context}

---

Solicitud: {query}

INSTRUCCIÓN OBLIGATORIA: Esto es una búsqueda en documentos internos del usuario, NO una consulta de normativa aduanera. Las reglas de "fuera de ámbito" NO aplican aquí. Muestra directamente la información del documento encontrado: fechas, emisores, RUT, montos, ítems, y cualquier otro dato presente. Organiza la información de forma clara y estructurada."""

        type_hints = {
            "arancelaria": "Esta es una consulta sobre clasificación arancelaria o tarifas.",
            "tramite": "Esta es una consulta sobre procedimientos o trámites aduaneros.",
            "normativa": "Esta es una consulta sobre normativa, circulares o resoluciones aduaneras.",
            "general": "",
        }
        hint = type_hints.get(query_type, "")

        if context:
            message = f"""[CONTEXTO] — Documentos oficiales recuperados de la base de datos:

{context}

---

{hint}
Consulta del usuario: {query}

Instrucciones:
- Responde ÚNICAMENTE con información que esté explícita en el [CONTEXTO] anterior.
- Cita cada fuente con precisión (nombre, número, fecha).
- Si el [CONTEXTO] no contiene la respuesta completa, indica exactamente qué parte falta y aplica la REGLA DE ORO: no deduzcas ni completes con conocimiento propio."""
        else:
            message = f"""{hint}
Consulta del usuario: {query}

No hay documentos sobre este tema en la base de datos en este momento.

Aplica la REGLA DE ORO: responde ÚNICAMENTE:
"La información solicitada no se encuentra en los documentos disponibles en la base de datos. Para verificar, consulte directamente https://www.aduana.cl o https://www.diariooficial.interior.gob.cl"
No agregues ninguna información adicional, recomendación ni contexto de tu conocimiento propio."""

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
                temperature=LLM_TEMPERATURE,
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
        document_id: str | None = None,
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
            # Nunca cachear respuestas ancladas a un documento específico
            if document_id:
                return None
            if self._extract_title_fragment(query):
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
            asyncio.to_thread(self._retrieve_documents, query, query_type, filter_collection, user_id, document_id),
        )

        # Detectar si hay documentos internos recuperados por coincidencia de título
        # (distance=0.0 es la marca que usa get_chunks_by_title_fragment)
        # También aplica cuando hay document_id activo (todos sus chunks son relevantes)
        has_internal_results = bool(document_id) or any(
            r.get("distance", 1.0) == 0.0 and r.get("collection") == COLLECTION_INTERNOS
            for r in top_results
        )

        # No usar caché cuando hay docs internos
        if cached and not has_internal_results:
            return {
                "answer": cached["answer"],
                "sources": cached["sources"],
                "query_type": cached["query_type"],
                "total_chunks_retrieved": 0,
                "cache_hit": True,
            }

        # Short-circuit: sin contexto → respuesta hardcodeada, sin llamar al LLM
        if not context_text:
            return {
                "answer": NOT_FOUND_RESPONSE,
                "sources": [],
                "query_type": query_type,
                "total_chunks_retrieved": 0,
            }

        # 3. Construir mensajes para Claude
        user_message = self._build_user_message(query, context_text, query_type, has_internal_results)
        messages = [*history, {"role": "user", "content": user_message}]

        # 4. Llamar a Claude (cliente async)
        answer = ""
        selected_model = self._select_model(query)
        try:
            client = self._get_async_anthropic_client()
            _t_llm = time.perf_counter()
            response = await client.messages.create(
                model=selected_model,
                max_tokens=4096,
                temperature=LLM_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            _llm_ms = (time.perf_counter() - _t_llm) * 1000
            answer = response.content[0].text
            log_llm_call(
                model=selected_model,
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
                duration_ms=_llm_ms,
                query_type=query_type,
                document_id=document_id,
            )
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

        # No cachear respuestas con documentos internos: son específicas por usuario
        if answer and not answer.startswith("Error:") and filter_collection == "all" and not has_internal_results:
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
                temperature=LLM_TEMPERATURE,
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
        document_id: str | None = None,
    ) -> tuple[list[dict], str, list[dict]]:
        """
        Recupera documentos del vector store, filtra y construye contexto.
        Retorna (top_results, context_text, sources).

        Búsqueda híbrida:
        1. Si hay document_id activo: búsqueda estrictamente en ese documento (early-return).
        2. Siempre intenta búsqueda por título en internos si el filtro lo permite.
        3. Si la consulta tiene señales explícitas de documento interno, fuerza
           la búsqueda semántica a INTERNOS únicamente.
        4. Los resultados por título se anteponen con relevancia máxima.
        """
        # Paso 0: document_id activo → búsqueda aislada exclusivamente en ese documento
        if document_id:
            try:
                results = self.vector_store.search(
                    query=query,
                    collection_name=COLLECTION_INTERNOS,
                    top_k=TOP_K_INTERNOS,
                    filter_metadata={"doc_id": document_id},
                )
                logger.info(
                    f"Búsqueda anclada a doc '{document_id}': {len(results)} chunks"
                )
                context_text, sources = self._build_context(results)
                return results, context_text, sources
            except Exception as e:
                logger.error(f"Error en búsqueda por document_id '{document_id}': {e}")
                # fallback a búsqueda normal si el filtro falla

        # Paso 1: búsqueda por título en internos (aditiva, no excluye normativa)
        title_results = []
        if filter_collection in ("all", "internos"):
            title_fragment = self._extract_title_fragment(query)
            if title_fragment:
                try:
                    title_results = self.vector_store.get_chunks_by_title_fragment(
                        title_fragment, COLLECTION_INTERNOS, user_id=user_id
                    )
                    if title_results:
                        logger.info(
                            f"Búsqueda por título '{title_fragment}': "
                            f"{len(title_results)} chunks encontrados"
                        )
                except Exception as e:
                    logger.warning(f"Error en búsqueda por título: {e}")

        # Paso 2: si la consulta pide explícitamente documentos internos,
        # la búsqueda semántica se restringe a esa colección
        is_internal_only = self._is_internal_doc_query(query) and filter_collection == "all"
        effective_filter = "internos" if is_internal_only else filter_collection
        collection_strategy = self._determine_collection_priority(query_type, effective_filter)

        # Paso 3: búsqueda semántica
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

        # Paso 4: anteponer resultados por título (distance=0 → máxima prioridad)
        if title_results:
            # Ya tenemos el documento específico por título — no mezclar otros
            # documentos internos del semántico; solo agregar resultados normativos
            seen_texts = {r["text"] for r in title_results}
            normativa_only = [
                r for r in all_results
                if r.get("collection") != COLLECTION_INTERNOS
                and r["text"] not in seen_texts
            ]
            all_results = title_results + normativa_only
        elif title_fragment and filter_collection == "all":
            # Paso 4b: fallback — búsqueda semántica dedicada en internos cuando el
            # title search no encontró coincidencias (puede ser discrepancia en metadatos)
            try:
                meta_filter = {"user_id": user_id} if user_id else None
                fallback = self.vector_store.search(
                    query=query,
                    collection_name=COLLECTION_INTERNOS,
                    top_k=TOP_K_RESULTS,
                    filter_metadata=meta_filter,
                )
                if fallback:
                    # Identificar el documento más relevante (primer resultado)
                    best_meta = fallback[0].get("metadata", {})
                    best_doc_id = best_meta.get("doc_id") or best_meta.get("filename", "")
                    # Incluir todos los chunks del mismo documento
                    best_chunks = [
                        r for r in fallback
                        if (r.get("metadata", {}).get("doc_id") or
                            r.get("metadata", {}).get("filename", "")) == best_doc_id
                    ] if best_doc_id else fallback[:1]
                    for r in best_chunks:
                        r["distance"] = 0.0
                    seen_texts = {r["text"] for r in best_chunks}
                    normativa_only = [r for r in all_results if r["text"] not in seen_texts]
                    all_results = best_chunks + normativa_only
                    logger.info(
                        f"Fallback semántico en internos: {len(fallback)} chunks disponibles, "
                        f"usando {len(best_chunks)} del doc '{best_doc_id}'"
                    )
            except Exception as e:
                logger.warning(f"Error en fallback semántico internos: {e}")

        # Paso 5: filtro de distancia solo para búsquedas mixtas (all)
        # Los title_results (distance=0) siempre pasan este filtro
        if effective_filter == "all":
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
        # Usar cap mayor cuando se encontró un documento interno específico por título
        # (puede tener hasta 10+ chunks en documentos legales extensos)
        max_chunks = 10 if title_results else 6
        top_results = all_results[:max_chunks]
        context_text, sources = self._build_context(top_results)
        return top_results, context_text, sources

    async def query_stream(
        self,
        query: str,
        filter_collection: str = "all",
        user_id: str | None = None,
        session_id: str | None = None,
        document_id: str | None = None,
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
            # Nunca cachear respuestas ancladas a un documento específico
            if document_id:
                return None
            if self._extract_title_fragment(query):
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
            asyncio.to_thread(self._retrieve_documents, query, query_type, filter_collection, user_id, document_id),
        )

        # Detectar documentos internos recuperados por título (distance=0.0)
        # También aplica cuando hay document_id activo
        has_internal_results = bool(document_id) or any(
            r.get("distance", 1.0) == 0.0 and r.get("collection") == COLLECTION_INTERNOS
            for r in top_results
        )

        # No usar caché para consultas con documentos internos
        if cached and not has_internal_results:
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

        # Short-circuit: sin contexto → respuesta hardcodeada, sin llamar al LLM
        if not context_text:
            yield _sse({"type": "token", "text": NOT_FOUND_RESPONSE})
            yield _sse({"type": "done", "sources": [], "query_type": query_type, "chunks": 0})
            return

        yield _sse({"type": "stage", "text": "Generando respuesta..."})

        user_message = self._build_user_message(query, context_text, query_type, has_internal_results)
        messages = [*history, {"role": "user", "content": user_message}]

        full_answer = ""
        _stream_model = self._select_model(query)
        try:
            client = self._get_async_anthropic_client()
            _t_llm = time.perf_counter()
            async with client.messages.stream(
                model=_stream_model,
                max_tokens=4096,
                temperature=LLM_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    full_answer += text
                    yield _sse({"type": "token", "text": text})
                # Capturar tokens al finalizar el stream
                try:
                    _final = await stream.get_final_message()
                    _llm_ms = (time.perf_counter() - _t_llm) * 1000
                    log_llm_call(
                        model=_stream_model,
                        prompt_tokens=_final.usage.input_tokens,
                        completion_tokens=_final.usage.output_tokens,
                        duration_ms=_llm_ms,
                        query_type=query_type,
                        document_id=document_id,
                    )
                except Exception as _te:
                    logger.warning(f"[stream] No se pudo capturar uso de tokens: {_te}")

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

        # No cachear respuestas con documentos internos
        if full_answer and filter_collection == "all" and not has_internal_results:
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
