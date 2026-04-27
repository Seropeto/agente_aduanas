"""
Motor RAG (Retrieval-Augmented Generation) para el Agente de Aduanas Chile.
Integra ChromaDB para recuperación de documentos y Claude para generación de respuestas.
"""
import logging
import re
from typing import Any, Optional

import anthropic

from backend.config import ANTHROPIC_API_KEY, MODEL_NAME, TOP_K_RESULTS

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

SYSTEM_PROMPT = """Eres un asistente especializado en aduanas y comercio exterior de Chile, con profundo conocimiento de:

- La Ordenanza de Aduanas (DFL N°30 de 2005) y sus modificaciones
- El Arancel Aduanero Chileno y la clasificación arancelaria (Sistema Armonizado)
- Circulares y Resoluciones del Servicio Nacional de Aduanas de Chile
- Procedimientos de importación y exportación en Chile
- Normativa tributaria aduanera: IVA a las importaciones, derechos aduaneros, impuesto adicional
- Acuerdos de libre comercio vigentes en Chile y sus normas de origen
- Zonas francas y regímenes aduaneros especiales
- Legislación del Servicio de Impuestos Internos (SII) relacionada con importaciones

## Importante sobre tus capacidades:

Este sistema cuenta con un scraper que descarga y actualiza automáticamente documentos desde:
- **Servicio Nacional de Aduanas** (aduana.cl) — Circulares, Resoluciones, Arancel, Procedimientos
- **Biblioteca del Congreso Nacional** (leychile.cl) — Ordenanza de Aduanas, Leyes, Decretos
- **Diario Oficial** (diariooficial.interior.gob.cl) — Resoluciones y Decretos de Aduanas, SII y Hacienda publicados en los últimos 14 días

Cuando se te proporciona contexto de documentos, esos documentos son REALES y ACTUALIZADOS, obtenidos directamente desde las fuentes oficiales. NUNCA digas que no tienes acceso a información reciente si se te proporcionan documentos en el contexto — esos documentos son tu fuente de información actualizada.

## Instrucciones de respuesta:

1. **SIEMPRE responde en español**, usando terminología técnica aduanera chilena correcta.
2. **Cuando se te proporciona contexto de documentos indexados:** basa tu respuesta en esos documentos. Cita las fuentes con precisión (nombre, número de circular/resolución, artículo, fecha).
3. **Cuando no hay documentos en el contexto:** responde con toda tu expertise en normativa aduanera chilena. JAMÁS menciones fechas de corte, limitaciones de entrenamiento, ni expliques cómo funciona la IA. Simplemente responde como el experto que eres.
4. **PROHIBIDO ABSOLUTO:** mencionar "corte de entrenamiento", "fecha de corte", "conocimiento hasta X año", "no tengo acceso en tiempo real", o cualquier variante. Esas frases destruyen la confianza del usuario. Si no tienes un documento específico, orienta al usuario a las fuentes oficiales sin explicar por qué.
5. Cuando menciones un número de circular, resolución o decreto, indícalo explícitamente (ej. "Circular N°XX de Aduanas", "Resolución Exenta N°XX").
6. Estructura tus respuestas de forma clara con párrafos o listas cuando sea apropiado.
7. Si se trata de un procedimiento, describe los pasos en orden.
8. Para consultas arancelarias, menciona la partida o subpartida del SA cuando sea relevante.
9. Mantén un tono profesional y técnico, apropiado para agencias de aduanas.

## Formato de citas:
Al final de tu respuesta, si usaste fuentes del contexto, inclúyelas como:
**Fuentes consultadas:** [lista de documentos citados]
"""


class RAGEngine:
    """
    Motor RAG que combina búsqueda vectorial con generación de Claude.
    """

    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store
        self._anthropic_client: Optional[anthropic.Anthropic] = None

    def _get_anthropic_client(self) -> anthropic.Anthropic:
        """Retorna el cliente de Anthropic, creándolo si es necesario."""
        if not self._anthropic_client:
            if not ANTHROPIC_API_KEY:
                raise ValueError(
                    "ANTHROPIC_API_KEY no configurada. "
                    "Agregue su clave API en el archivo .env"
                )
            self._anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return self._anthropic_client

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

    async def query(
        self,
        query: str,
        filter_collection: str = "all",
        user_id: str | None = None,
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

        # 1. Clasificar la consulta
        query_type = self.classify_query(query)
        logger.info(f"Consulta clasificada como: {query_type}")

        # 2. Determinar estrategia de búsqueda
        collection_strategy = self._determine_collection_priority(
            query_type, filter_collection
        )

        # 3. Recuperar documentos relevantes
        all_results = []
        for collection_name, top_k in collection_strategy:
            try:
                # Filtrar documentos internos por usuario
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

        # Filtrar por relevancia mínima antes de ordenar.
        # En modo "all", los documentos internos solo se incluyen si superan
        # el umbral de similitud; evita ruido con 10.000+ documentos internos.
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

        # Ordenar por relevancia global
        all_results.sort(key=lambda x: x.get("distance", 1.0))

        # Tomar los mejores resultados (máximo 6 para no exceder el contexto)
        top_results = all_results[:6]

        # 4. Construir contexto y fuentes
        context_text, sources = self._build_context(top_results)

        # 5. Construir mensaje para Claude
        user_message = self._build_user_message(query, context_text, query_type)

        # 6. Llamar a Claude
        try:
            client = self._get_anthropic_client()
            response = client.messages.create(
                model=MODEL_NAME,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_message}
                ],
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
        except Exception as e:
            logger.error(f"Error llamando a la API de Anthropic: {e}")
            answer = (
                f"Ocurrió un error al procesar su consulta: {str(e)}. "
                "Por favor, intente nuevamente."
            )

        return {
            "answer": answer,
            "sources": sources,
            "query_type": query_type,
            "total_chunks_retrieved": len(top_results),
        }
