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

from backend.config import DATA_DIR, LOGS_DIR

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
    from backend.scrapers import AduanaScraper, BCNScraper, SIIScraper, DiarioOficialScraper
    from backend.indexer.document_processor import DocumentProcessor
    from backend.indexer.vectorstore import VectorStore, COLLECTION_NORMATIVA

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
        scrapers = [
            ("Aduana.cl", AduanaScraper()),
            ("BCN.cl", BCNScraper()),
            ("SII.cl", SIIScraper()),
            ("Diario Oficial", DiarioOficialScraper()),
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
                        url = doc.get("url", "")
                        content = doc.get("content", "")

                        if not content or len(content.strip()) < 50:
                            continue

                        # Verificar si ya está indexado
                        if url and vector_store.is_document_indexed(
                            url, id_type="url", collection_name=COLLECTION_NORMATIVA
                        ):
                            continue

                        # Verificar por hash de contenido
                        content_hash = processor.compute_text_hash(content)
                        if vector_store.is_document_indexed(
                            content_hash, id_type="doc_id", collection_name=COLLECTION_NORMATIVA
                        ):
                            continue

                        # Procesar y crear chunks
                        metadata = {
                            "title": doc.get("title", "Sin título"),
                            "url": url,
                            "date": doc.get("date", ""),
                            "content_type": doc.get("content_type", "normativa"),
                            "source": doc.get("source", scraper_name),
                            "doc_id": content_hash[:16],
                        }

                        chunks = processor.process_text(content, metadata)

                        if chunks:
                            added = vector_store.add_documents(
                                chunks, COLLECTION_NORMATIVA
                            )
                            if added > 0:
                                scraper_docs += 1
                                total_docs_added += 1

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
