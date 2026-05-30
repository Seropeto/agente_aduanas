"""
Planificador de tareas para el scraping automático de fuentes normativas.
Usa APScheduler con AsyncIOScheduler para ejecución periódica.
"""
import asyncio
import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from backend.config import ANTHROPIC_API_KEY, DATA_DIR, LOGS_DIR

logger = logging.getLogger(__name__)

STATUS_FILE = DATA_DIR / "scheduler_status.json"
SCRAPER_LOG_FILE = LOGS_DIR / "scraper.log"

# Singleton del scheduler
_scheduler: Optional[AsyncIOScheduler] = None
_scraper_running = False


def _setup_scraper_logger():
    """Configura el logger para los scrapers con rotación de archivos."""
    scraper_logger = logging.getLogger("scraper_job")
    scraper_logger.setLevel(logging.INFO)

    if not scraper_logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            str(SCRAPER_LOG_FILE),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        scraper_logger.addHandler(handler)

        # También al console
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        scraper_logger.addHandler(console_handler)

    return scraper_logger


def _load_status() -> dict[str, Any]:
    """Carga el estado del scheduler desde el archivo JSON."""
    try:
        if STATUS_FILE.exists():
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"No se pudo cargar el estado del scheduler: {e}")

    return {
        "last_run": None,
        "next_run": None,
        "last_status": "nunca_ejecutado",
        "total_docs_indexed": 0,
        "last_run_docs": 0,
        "last_error": None,
        "is_running": False,
    }


def _save_status(status: dict[str, Any]):
    """Guarda el estado del scheduler en el archivo JSON."""
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.error(f"No se pudo guardar el estado del scheduler: {e}")


async def _generate_doc_summary(title: str, content: str) -> str:
    """
    Genera un resumen de 80 palabras del documento usando Claude Haiku.
    Retorna cadena vacía si la API no está disponible.
    """
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "Resume en máximo 80 palabras este documento de normativa aduanera chilena. "
                    "Incluye: tipo de norma, número/identificador (si aplica), tema principal y fecha. "
                    "Solo el resumen, sin prefijos ni explicaciones.\n\n"
                    f"TÍTULO: {title}\n\nCONTENIDO:\n{content}"
                ),
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"No se pudo generar resumen para '{title}': {e}")
        return ""


async def _run_all_scrapers() -> dict[str, Any]:
    """
    Ejecuta todos los scrapers e indexa los documentos obtenidos.
    Retorna un resumen de la ejecución.
    """
    global _scraper_running

    if _scraper_running:
        logger.warning("Ya hay un scraping en ejecución, saltando...")
        return {"status": "en_ejecucion", "docs_added": 0}

    _scraper_running = True
    scraper_log = _setup_scraper_logger()

    # Importaciones tardías para evitar ciclos
    from backend.scrapers import AduanaScraper, BCNScraper, SIIScraper, DiarioOficialScraper, TLCScraper
    from backend.indexer.document_processor import DocumentProcessor
    from backend.indexer.vectorstore import VectorStore, COLLECTION_NORMATIVA
    from backend.normative_changelog import (
        add_change, get_stored_hash, upsert_document_hash,
    )

    start_time = datetime.now(timezone.utc)
    scraper_log.info("=" * 60)
    scraper_log.info(f"Iniciando scraping automático: {start_time.isoformat()}")
    scraper_log.info("=" * 60)

    # Actualizar estado a "ejecutando"
    status = _load_status()
    status["is_running"] = True
    status["last_run"] = start_time.isoformat()
    _save_status(status)

    total_docs_added = 0
    errors = []
    scrapers_summary = {}

    processor = DocumentProcessor()
    vector_store = VectorStore()

    try:
        vector_store.initialize()

        # Lista de scrapers a ejecutar
        # Nota: SII.cl y Diario Oficial están deshabilitados temporalmente debido a cambios
        # en la estructura de sus sitios web. Solo los scrapers funcionales están activos.
        scrapers = [
            ("Aduana.cl", AduanaScraper()),
            ("BCN.cl", BCNScraper()),
            ("Diario Oficial", DiarioOficialScraper()),
            ("TLC/SUBREI", TLCScraper()),
            # ("SII.cl", SIIScraper()),  # Deshabilitado: URLs retornan 404
        ]

        for scraper_name, scraper in scrapers:
            scraper_log.info(f"Iniciando scraper: {scraper_name}")
            scraper_docs = 0

            try:
                documents = await scraper.scrape()
                scraper_log.info(
                    f"{scraper_name}: {len(documents)} documentos obtenidos"
                )

                for doc in documents:
                    try:
                        url     = doc.get("url", "")
                        content = doc.get("content", "")
                        title   = doc.get("title", "Sin título")
                        doc_date = doc.get("date", "")

                        if not content or len(content.strip()) < 50:
                            continue

                        new_hash = processor.compute_text_hash(content)
                        doc_id   = new_hash[:16]

                        # ── Detectar si es nuevo o modificación ──────────────
                        stored    = get_stored_hash(url) if url else None
                        is_new    = stored is None
                        is_changed = stored is not None and stored["content_hash"] != new_hash

                        # Si no cambió nada, saltar
                        if not is_new and not is_changed:
                            continue

                        change_type = "incorporacion" if is_new else "modificacion"
                        prev_id     = None

                        # Si es modificación, eliminar chunks anteriores de ChromaDB
                        if is_changed:
                            old_doc_id = stored["doc_id"]
                            try:
                                vector_store.delete_document(old_doc_id, COLLECTION_NORMATIVA)
                                scraper_log.info(
                                    f"Chunks anteriores eliminados de ChromaDB: {old_doc_id}"
                                )
                            except Exception as e:
                                scraper_log.warning(f"No se pudieron eliminar chunks viejos: {e}")

                        # Procesar y crear chunks
                        metadata = {
                            "title":        title,
                            "url":          url,
                            "date":         doc_date,
                            "content_type": doc.get("content_type", "normativa"),
                            "source":       doc.get("source", scraper_name),
                            "doc_id":       doc_id,
                            "change_type":  change_type,
                            "version":      "1.0",
                            "is_active":    "1",
                        }

                        chunks = processor.process_text(content, metadata)

                        if chunks:
                            added = vector_store.add_documents(chunks, COLLECTION_NORMATIVA)
                            if added > 0:
                                scraper_docs += 1
                                total_docs_added += 1

                                # ── Generar resumen con Haiku ─────────────────
                                summary = await _generate_doc_summary(title, content[:2000])

                                # ── Registrar en changelog ────────────────────
                                prev_id = add_change(
                                    document_id=doc_id,
                                    title=title,
                                    change_type=change_type,
                                    change_date=doc_date or start_time.strftime("%Y-%m-%d"),
                                    source_url=url,
                                    summary=summary,
                                    content_hash=new_hash,
                                    sector="aduanas",
                                    previous_version_id=prev_id,
                                )

                                # ── Actualizar hash almacenado ────────────────
                                if url:
                                    upsert_document_hash(
                                        url=url,
                                        content_hash=new_hash,
                                        doc_id=doc_id,
                                        title=title,
                                        sector="aduanas",
                                    )

                                scraper_log.info(
                                    f"[{change_type.upper()}] {title} — {url or 'sin URL'}"
                                )

                    except Exception as e:
                        logger.error(f"Error procesando doc de {scraper_name}: {e}")

                scrapers_summary[scraper_name] = {
                    "obtenidos": len(documents),
                    "indexados": scraper_docs,
                    "status": "completado",
                }
                scraper_log.info(
                    f"{scraper_name}: {scraper_docs} documentos nuevos indexados"
                )

            except Exception as e:
                error_msg = f"{scraper_name}: Error - {str(e)}"
                errors.append(error_msg)
                scraper_log.error(error_msg)
                scrapers_summary[scraper_name] = {
                    "obtenidos": 0,
                    "indexados": 0,
                    "status": f"error: {str(e)}",
                }

        stats = vector_store.get_stats()
        total_in_store = stats.get("total", 0)

    except Exception as e:
        error_msg = f"Error crítico en scraping: {str(e)}"
        logger.error(error_msg)
        errors.append(error_msg)
        total_in_store = 0

    finally:
        vector_store.close()
        _scraper_running = False

    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    scraper_log.info(f"Scraping completado en {duration:.1f}s")
    scraper_log.info(f"Total documentos nuevos indexados: {total_docs_added}")
    scraper_log.info(f"Total en base de datos: {total_in_store}")
    if errors:
        scraper_log.warning(f"Errores: {len(errors)}")
        for err in errors:
            scraper_log.warning(f"  - {err}")

    # Calcular próxima ejecución
    global _scheduler
    next_run_str = None
    if _scheduler:
        job = _scheduler.get_job("weekly_scraper")
        if job and job.next_run_time:
            next_run_str = job.next_run_time.isoformat()

    # Guardar estado final
    final_status = {
        "last_run": start_time.isoformat(),
        "last_run_end": end_time.isoformat(),
        "next_run": next_run_str,
        "last_status": "error" if errors and total_docs_added == 0 else "completado",
        "total_docs_indexed": total_in_store,
        "last_run_docs": total_docs_added,
        "last_error": errors[-1] if errors else None,
        "is_running": False,
        "duration_seconds": round(duration, 1),
        "scrapers_summary": scrapers_summary,
    }
    _save_status(final_status)

    return final_status


async def _scheduled_job():
    """Trabajo programado que ejecuta el scraping semanal."""
    logger.info("Ejecutando trabajo programado de scraping semanal")
    try:
        await _run_all_scrapers()
    except Exception as e:
        logger.error(f"Error en trabajo programado: {e}")


async def _monthly_reset_job():
    """Trabajo programado que resetea cuotas mensuales el día 1 de cada mes."""
    logger.info("Ejecutando reset mensual de cuotas de usuarios")
    try:
        from backend.billing.service import run_monthly_reset
        count = run_monthly_reset()
        logger.info(f"Reset mensual completado: {count} usuarios reseteados")
    except Exception as e:
        logger.error(f"Error en reset mensual de cuotas: {e}")


def start_scheduler():
    """Inicia el scheduler con el trabajo semanal de scraping."""
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.info("El scheduler ya está en ejecución")
        return

    _setup_scraper_logger()
    _scheduler = AsyncIOScheduler(timezone="America/Santiago")

    # Ejecutar cada 7 días
    _scheduler.add_job(
        _scheduled_job,
        trigger=IntervalTrigger(weeks=1),
        id="weekly_scraper",
        name="Scraping semanal de normativa aduanera",
        replace_existing=True,
        max_instances=1,
    )

    # Reset mensual de cuotas — día 1 de cada mes a las 00:05
    _scheduler.add_job(
        _monthly_reset_job,
        trigger=CronTrigger(day=1, hour=0, minute=5, timezone="America/Santiago"),
        id="monthly_quota_reset",
        name="Reset mensual de cuotas de usuarios",
        replace_existing=True,
        max_instances=1,
    )

    _scheduler.start()
    logger.info("Scheduler iniciado: scraping semanal programado")

    # Actualizar status con próxima ejecución
    status = _load_status()
    job = _scheduler.get_job("weekly_scraper")
    if job and job.next_run_time:
        status["next_run"] = job.next_run_time.isoformat()
    _save_status(status)


def stop_scheduler():
    """Detiene el scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido")


async def run_now() -> dict[str, Any]:
    """
    Ejecuta el scraping de forma inmediata (fuera del horario programado).
    Retorna el resultado de la ejecución.
    """
    logger.info("Ejecutando scraping manual")
    return await _run_all_scrapers()


def get_status() -> dict[str, Any]:
    """
    Retorna el estado actual del scheduler y último scraping.
    """
    global _scheduler, _scraper_running

    status = _load_status()
    status["scheduler_running"] = bool(_scheduler and _scheduler.running)
    status["scraper_currently_running"] = _scraper_running

    # Actualizar next_run desde el scheduler si está activo
    if _scheduler and _scheduler.running:
        job = _scheduler.get_job("weekly_scraper")
        if job and job.next_run_time:
            status["next_run"] = job.next_run_time.isoformat()

    return status
