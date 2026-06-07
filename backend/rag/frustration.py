"""
AGENT-003 — System Prompt externalizado + Protocolo de Frustración Elegante.

Dos responsabilidades:
  1. Cargar el System Prompt desde un archivo independiente (NUNCA hardcoded).
     Fuente canónica: backend/prompts/system_prompt.txt (override por env SYSTEM_PROMPT_FILE).
  2. Control de alcance: si la operación / DIN / país consultado NO está entre las
     operaciones cargadas en el contexto, se interrumpe el análisis y se devuelve
     EXACTAMENTE la plantilla corporativa de frustración elegante (texto determinista,
     garantizado por código — no se delega su literalidad al LLM).
"""
import os
import re
import logging
from pathlib import Path

import anthropic

from backend.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Modelo para el dictamen en alcance. Respeta el incidente temporal (Haiku) vía env.
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-haiku-4-5-20251001")

_PROMPT_PATH = os.getenv(
    "SYSTEM_PROMPT_FILE",
    str(Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"),
)

# Plantilla EXACTA exigida por el Director. {requested} y {available} se sustituyen
# por código; el resto del texto es inmutable.
FRUSTRATION_TEMPLATE = (
    "La consulta solicitada se encuentra fuera del alcance del entorno actual de "
    "Agentia. El análisis de operaciones para {requested} requiere la inyección "
    "previa de la Carpeta de Despacho y documentación de respaldo en el sistema. "
    "Actualmente, solo se encuentran validadas las operaciones asociadas a {available}."
)

# Países reconocibles en la consulta para el control de alcance.
KNOWN_COUNTRIES = [
    "India", "Rusia", "China", "España", "Estados Unidos", "Brasil", "Argentina",
    "Alemania", "Japón", "Francia", "Italia", "México", "Colombia", "Perú",
    "Corea del Sur", "Taiwán", "Turquía", "Vietnam", "Tailandia", "Indonesia",
]

_DIN_RE = re.compile(r"\b\d{4}-din-\d+\b", re.IGNORECASE)

_client: "anthropic.AsyncAnthropic | None" = None


# ── System Prompt externalizado ───────────────────────────────────────────────

def load_system_prompt() -> str:
    """Lee el System Prompt desde el archivo independiente. Lanza si no existe
    (el prompt NUNCA debe estar hardcoded como fuente de verdad)."""
    with open(_PROMPT_PATH, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise RuntimeError(f"System Prompt vacío en {_PROMPT_PATH}")
    return text


# ── Protocolo de frustración elegante ─────────────────────────────────────────

def _format_available(available_labels) -> str:
    labels = [str(x).strip() for x in (available_labels or []) if str(x).strip()]
    if not labels:
        return "ninguna operación cargada actualmente"
    # de-duplica preservando orden
    seen, uniq = set(), []
    for l in labels:
        k = l.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(l)
    return ", ".join(uniq)


def build_frustration_response(requested_label: str, available_labels) -> str:
    """Devuelve la plantilla EXACTA con los marcadores sustituidos (determinista)."""
    requested = (requested_label or "la operación solicitada").strip()
    return FRUSTRATION_TEMPLATE.format(
        requested=requested,
        available=_format_available(available_labels),
    )


def _label_available(label: str, available_labels) -> bool:
    l = (label or "").strip().lower()
    return any(l == str(x).strip().lower() for x in (available_labels or []))


def detect_scope(query: str, available_countries, available_operations):
    """
    Determina si la consulta cae dentro del alcance del contexto cargado.

    Devuelve (in_scope: bool, requested_label: str | None).
      - Si la consulta menciona una operación/país NO disponible y ninguna disponible
        → (False, "<lo solicitado>").
      - Si menciona algo disponible o nada específico → (True, None).
    """
    q = (query or "").lower()
    avail_c = {str(c).strip().lower() for c in (available_countries or [])}
    avail_o = {str(o).strip().lower() for o in (available_operations or [])}

    dins = [d.lower() for d in _DIN_RE.findall(q)]
    din_in_scope = any(d in avail_o for d in dins)
    din_out = next((d for d in dins if d not in avail_o), None)

    country_in_scope = False
    country_out = None
    for c in KNOWN_COUNTRIES:
        if re.search(r"\b" + re.escape(c.lower()) + r"\b", q):
            if c.lower() in avail_c:
                country_in_scope = True
            elif country_out is None:
                country_out = c

    if din_in_scope or country_in_scope:
        return True, None
    if din_out:
        return False, din_out.upper()
    if country_out:
        return False, country_out
    return True, None  # sin referencia explícita fuera de alcance


# ── Orquestación con protocolo ────────────────────────────────────────────────

def _get_client() -> "anthropic.AsyncAnthropic":
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY no configurada")
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


async def analyze_with_protocol(
    query: str,
    context_text: str,
    available_labels,
    requested_label: str | None = None,
    model: str | None = None,
) -> str:
    """
    Aplica el protocolo y, si la operación está en alcance, genera el dictamen con el
    System Prompt externalizado.

      - requested_label fuera de available_labels → plantilla exacta (sin LLM).
      - contexto vacío → plantilla exacta (sin LLM).
      - en alcance → dictamen del LLM con el prompt cargado desde archivo.
    """
    if requested_label is not None and not _label_available(requested_label, available_labels):
        logger.info("Protocolo frustración: '%s' fuera de alcance", requested_label)
        return build_frustration_response(requested_label, available_labels)

    if not (context_text or "").strip():
        logger.info("Protocolo frustración: contexto vacío para '%s'", requested_label)
        return build_frustration_response(requested_label or "la operación solicitada", available_labels)

    client = _get_client()
    resp = await client.messages.create(
        model=model or AGENT_MODEL,
        max_tokens=1500,
        temperature=0,
        system=load_system_prompt(),
        messages=[{
            "role": "user",
            "content": f"CONTEXTO:\n{context_text}\n\nCONSULTA DEL USUARIO:\n{query}",
        }],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
