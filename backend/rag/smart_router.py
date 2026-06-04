"""
SmartRouter — Ticket 002

Interceptor de enrutamiento inteligente para POST /api/chat/stream.

Clasifica cada consulta del usuario (vía Claude Haiku como clasificador ligero,
con fallback heurístico determinista) y la despacha al modelo óptimo:

  SIMPLE  → RAG estándar sobre ChromaDB + Claude Haiku (barato, flujos lineales).
  COMPLEX → flujo híbrido: consulta PostgreSQL filtrando por user_id (Ticket 001)
            + vectores de ChromaDB → Claude Sonnet (preciso, análisis cruzado).

El router se diseña desacoplado y testeable:
  - client_factory: callable que retorna un cliente async de Anthropic (inyectable
    en tests con un fake).
  - El acceso a PostgreSQL se hace de forma lazy (import dentro de route()) para no
    acoplar el módulo a la capa de datos en tiempo de import.
"""
import calendar
import json
import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── Modelos ───────────────────────────────────────────────────────────────────
MODEL_ROUTER_CLASSIFIER = "claude-haiku-4-5-20251001"   # clasificador ligero
MODEL_SIMPLE_STREAM     = "claude-haiku-4-5-20251001"   # ruta SIMPLE
MODEL_COMPLEX_STREAM    = "claude-sonnet-4-6"            # ruta COMPLEX (análisis cruzado)

# ── Prompt rígido del clasificador ────────────────────────────────────────────
ROUTER_SYSTEM_PROMPT = """Eres un clasificador de intención para un asistente aduanero.
Clasifica la consulta del usuario en EXACTAMENTE una categoría.

Responde ÚNICAMENTE con un objeto JSON plano, sin texto adicional ni markdown:
{"type": "SIMPLE"} o {"type": "COMPLEX"}

SIMPLE: preguntas teóricas, definiciones de leyes, conceptos aduaneros directos,
explicaciones de un término. Ejemplos: "¿Qué es el DFL 30?", "¿Cómo se calcula el
IVA de una importación?".

COMPLEX: consultas que exijan auditar, comparar o revisar documentos internos del
usuario (facturas, declaraciones), cruzar datos relacionales, o que incluyan
variables explícitas como rangos de fechas, folios, montos, o análisis de criterios
aduaneros mixtos. Ejemplos: "Revisa las facturas entre octubre y marzo",
"Compara la clasificación de los Ray-Ban Meta en mis declaraciones"."""

# Señales léxicas para el fallback heurístico (cuando el clasificador LLM falla).
# OJO: "factura"/"declaración" SOLAS no implican COMPLEX (p.ej. "cómo se calcula el
# IVA de una factura" es doctrinario = SIMPLE). COMPLEX requiere verbos de auditoría,
# referencias posesivas a documentos del usuario, folios/montos o rangos temporales.
COMPLEX_SIGNALS = [
    "revisa", "revisar", "audita", "auditar", "auditoría", "auditoria",
    "compara", "comparar", "comparación", "cruza", "cruzar",
    "folio", "folios", "rango",
    "mis documentos", "mis facturas", "mis declaraciones",
    "ray-ban", "ray ban",
    "9026", "9032",  # conflicto de partida arancelaria → análisis cruzado
]

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


class SmartRouter:
    """Enrutador de intención SIMPLE/COMPLEX con despacho dinámico de modelo."""

    def __init__(self, client_factory: Optional[Callable[[], Any]] = None):
        """
        Args:
            client_factory: callable sin argumentos que retorna un cliente async
                de Anthropic. Si es None, classify_intent usa solo la heurística.
        """
        self._client_factory = client_factory

    # ── Clasificación ─────────────────────────────────────────────────────────

    def _heuristic_intent(self, query: str) -> str:
        """Clasificación determinista de respaldo (sin LLM)."""
        q = query.lower()
        if any(sig in q for sig in COMPLEX_SIGNALS):
            return "COMPLEX"
        # Rango temporal explícito: "entre ... 2025 ... 2026", "desde ... hasta ..."
        if re.search(r"\b(entre|desde|hasta)\b", q) and re.search(r"\b20\d{2}\b", q):
            return "COMPLEX"
        # Fechas explícitas dd/mm/aaaa
        if re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", q):
            return "COMPLEX"
        return "SIMPLE"

    async def classify_intent(self, query: str) -> str:
        """
        Clasifica la consulta como 'SIMPLE' o 'COMPLEX' usando Claude Haiku.
        Ante cualquier fallo, cae al clasificador heurístico determinista.
        """
        if not self._client_factory:
            return self._heuristic_intent(query)

        try:
            client = self._client_factory()
            resp = await client.messages.create(
                model=MODEL_ROUTER_CLASSIFIER,
                max_tokens=16,
                temperature=0.0,
                system=ROUTER_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": '{"type":"'},
                ],
            )
            text = (resp.content[0].text or "").upper()
            # Parse tolerante: el prefill garantiza que el modelo continúa con
            # SIMPLE"} o COMPLEX"}.
            if "COMPLEX" in text:
                return "COMPLEX"
            if "SIMPLE" in text:
                return "SIMPLE"
            return self._heuristic_intent(query)
        except Exception as e:
            logger.warning(f"[router] Clasificador LLM falló ({e}) — usando heurística")
            return self._heuristic_intent(query)

    # ── Extracción de filtros para PostgreSQL ─────────────────────────────────

    def _extract_pg_filters(self, query: str) -> dict[str, str]:
        """
        Extrae filtros estructurados de la consulta para acotar la query a PostgreSQL.
        Detecta tipo de documento y rangos de fechas en lenguaje natural español.
        """
        q = query.lower()
        filters: dict[str, str] = {}

        if "factura" in q:
            filters["content_type"] = "factura"
        elif "declaracion" in q or "declaración" in q:
            filters["content_type"] = "declaracion"

        # Rango de fechas por pares mes-año: "octubre de 2025 ... marzo de 2026"
        pares = re.findall(
            r"(" + "|".join(_MESES.keys()) + r")\s+(?:de\s+)?(20\d{2})", q
        )
        if len(pares) >= 2:
            (m1, y1), (m2, y2) = pares[0], pares[1]
            mes1, mes2 = _MESES[m1], _MESES[m2]
            ultimo_dia = calendar.monthrange(int(y2), mes2)[1]
            filters["fecha_desde"] = f"{y1}-{mes1:02d}-01"
            filters["fecha_hasta"] = f"{y2}-{mes2:02d}-{ultimo_dia:02d}"
        return filters

    @staticmethod
    def format_pg_context(documents: list[dict[str, Any]]) -> str:
        """Formatea la metadata de PostgreSQL como bloque de contexto para el LLM."""
        if not documents:
            return ""
        lines = ["<METADATA_DOCUMENTOS_USUARIO>"]
        for d in documents[:30]:
            lines.append(
                f"- {d.get('title', 'Sin título')} "
                f"(tipo: {d.get('content_type', '')}, "
                f"fecha: {d.get('fecha_documento') or 's/f'}, "
                f"doc_id: {d.get('doc_id', '')})"
            )
        lines.append("</METADATA_DOCUMENTOS_USUARIO>")
        return "\n".join(lines)

    # ── Enrutamiento ──────────────────────────────────────────────────────────

    async def route(self, query: str, user_id: str | None) -> dict[str, Any]:
        """
        Clasifica y prepara el despacho. Retorna una decisión de enrutamiento:
          {
            "intent": "SIMPLE" | "COMPLEX",
            "model": <model_id>,
            "pg_queried": bool,
            "pg_documents": list[dict],
            "pg_filters": dict,
            "classifier_ms": float,
          }

        Para COMPLEX se interroga PostgreSQL (filtrado OBLIGATORIO por user_id, ver
        Ticket 001) antes de delegar la generación a Sonnet.
        """
        t0 = time.perf_counter()
        intent = await self.classify_intent(query)
        classifier_ms = round((time.perf_counter() - t0) * 1000, 1)

        decision: dict[str, Any] = {
            "intent": intent,
            "model": MODEL_COMPLEX_STREAM if intent == "COMPLEX" else MODEL_SIMPLE_STREAM,
            "pg_queried": False,
            "pg_documents": [],
            "pg_filters": {},
            "classifier_ms": classifier_ms,
        }

        if intent == "COMPLEX" and user_id:
            filters = self._extract_pg_filters(query)
            decision["pg_filters"] = filters
            try:
                from backend.database import is_pg_enabled, query_documents
                if is_pg_enabled():
                    docs = await query_documents(user_id=user_id, limit=50, **filters)
                    decision["pg_queried"] = True
                    decision["pg_documents"] = docs
                    logger.info(
                        f"[router] COMPLEX → PostgreSQL (user={user_id}) "
                        f"filtros={filters} → {len(docs)} doc(s)"
                    )
                else:
                    logger.info("[router] COMPLEX pero PostgreSQL no está habilitado")
            except Exception as e:
                logger.warning(f"[router] Error consultando PostgreSQL: {e}")

        logger.info(
            f"[router] intent={intent} model={decision['model']} "
            f"pg_queried={decision['pg_queried']} ({classifier_ms}ms)"
        )
        return decision
