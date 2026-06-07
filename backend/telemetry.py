"""
Telemetría centralizada: Sentry + JSON logging estructurado + métricas RAG.
Toda la configuración se inyecta vía variables de entorno.

Variables relevantes:
    SENTRY_DSN              — DSN de Sentry (vacío = desactivado)
    SENTRY_TRACES_RATE      — Fracción de trazas de performance (0.0-1.0, default 0.2)
    LOG_LEVEL               — DEBUG | INFO | WARNING | ERROR (default INFO)
    LOG_FORMAT              — json | text (default json)
    ENVIRONMENT             — production | staging | development (default production)
    APP_VERSION             — versión del servicio (default 1.0.0)
    SERVICE_NAME            — nombre del servicio en los logs (default agentia-aduanas)
"""
import logging
import os
import time
from contextlib import contextmanager
from typing import Generator

# ── Configuración desde entorno ───────────────────────────────────────────────
SENTRY_DSN            = os.getenv("SENTRY_DSN", "")
SENTRY_TRACES_RATE    = float(os.getenv("SENTRY_TRACES_RATE", "0.2"))
LOG_LEVEL             = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT            = os.getenv("LOG_FORMAT", "json")   # "json" | "text"
ENVIRONMENT           = os.getenv("ENVIRONMENT", "production")
APP_VERSION           = os.getenv("APP_VERSION", "1.0.0")
SERVICE_NAME          = os.getenv("SERVICE_NAME", "agentia-aduanas")

# Costos aproximados Anthropic (USD / millón de tokens, ref. mayo 2025)
_TOKEN_COSTS = {
    "claude-haiku":  (0.80,  4.00),   # input, output
    "claude-sonnet": (3.00, 15.00),
    "claude-opus":   (15.0, 75.00),
}

_telemetry_logger = logging.getLogger("telemetry")


# ── Inicialización ────────────────────────────────────────────────────────────

def init_telemetry() -> None:
    """Punto de entrada único: configura logging y Sentry al arrancar la app."""
    _configure_logging()
    _init_sentry()
    _telemetry_logger.info(
        "Telemetría inicializada",
        extra={"event": "telemetry_init", "format": LOG_FORMAT, "sentry": bool(SENTRY_DSN)},
    )


def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Eliminar handlers previos para evitar duplicados en recarga
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()

    if LOG_FORMAT == "json":
        try:
            from pythonjsonlogger import jsonlogger

            class _ServiceFormatter(jsonlogger.JsonFormatter):
                def add_fields(self, log_record, record, message_dict):
                    super().add_fields(log_record, record, message_dict)
                    log_record["service"] = SERVICE_NAME
                    log_record["env"] = ENVIRONMENT
                    log_record["version"] = APP_VERSION

            handler.setFormatter(
                _ServiceFormatter(
                    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ",
                )
            )
        except ImportError:
            # Fallback silencioso a texto si la librería no está instalada
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    root.addHandler(handler)

    # Silenciar librerías ruidosas
    for noisy in ("uvicorn.access", "httpx", "httpcore", "openai", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _init_sentry() -> None:
    if not SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(level=logging.ERROR, event_level=logging.ERROR),
            ],
            traces_sample_rate=SENTRY_TRACES_RATE,
            environment=ENVIRONMENT,
            release=APP_VERSION,
            send_default_pii=False,
        )
        _telemetry_logger.info(
            "Sentry activo",
            extra={"event": "sentry_init", "environment": ENVIRONMENT, "traces_rate": SENTRY_TRACES_RATE},
        )
    except ImportError:
        _telemetry_logger.warning("sentry-sdk no instalado — captura de excepciones desactivada")
    except Exception as exc:
        _telemetry_logger.warning(f"Error iniciando Sentry: {exc}")


# ── Métricas LLM ──────────────────────────────────────────────────────────────

def log_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: float,
    query_type: str = "general",
    document_id: str | None = None,
    cached: bool = False,
) -> None:
    """Registra métricas de una llamada al LLM con costo estimado."""
    total_tokens = prompt_tokens + completion_tokens

    cost_in, cost_out = (0.80, 4.00)  # default Haiku
    for key, costs in _TOKEN_COSTS.items():
        if key in model:
            cost_in, cost_out = costs
            break
    cost_usd = (prompt_tokens * cost_in + completion_tokens * cost_out) / 1_000_000

    logging.getLogger("telemetry.llm").info(
        "llm_call",
        extra={
            "event":             "llm_call",
            "model":             model,
            "query_type":        query_type,
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      total_tokens,
            "cost_usd":          round(cost_usd, 6),
            "duration_ms":       round(duration_ms, 1),
            "cached":            cached,
            "document_id":       document_id,
        },
    )


