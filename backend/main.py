"""
Aplicación FastAPI principal de AgentIA Aduanas.
Provee endpoints de chat, gestión de documentos, control del scraper y archivos estáticos.
"""
import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import aiofiles
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import ADMIN_EMAIL, ADMIN_PASSWORD, BASE_DIR, LOGS_DIR, UPLOADS_DIR
from backend.indexer.document_processor import DocumentProcessor
from backend.indexer.vectorstore import VectorStore, COLLECTION_INTERNOS
from backend.query_logger import get_queries, get_summary, initialize_db, log_query
from backend.rag.engine import RAGEngine
from backend.scheduler import get_status, run_now, start_scheduler, stop_scheduler
from backend.auth.database import initialize_auth_db, admin_exists, create_user
from backend.auth.utils import hash_password
from backend.auth.router import router as auth_router, get_current_user
from backend.billing.router import router as billing_router

# ------------------------------------------------------------------ #
# Configuración de logging                                             #
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Instancias globales                                                  #
# ------------------------------------------------------------------ #
vector_store = VectorStore()
document_processor = DocumentProcessor()
rag_engine = RAGEngine(vector_store)

FRONTEND_DIR = BASE_DIR / "frontend"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


# ------------------------------------------------------------------ #
# Lifespan                                                             #
# ------------------------------------------------------------------ #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el inicio y apagado de la aplicación."""
    logger.info("Iniciando AgentIA Aduanas...")

    try:
        vector_store.initialize()
        logger.info("VectorStore inicializado correctamente")
    except Exception as e:
        logger.error(f"Error inicializando VectorStore: {e}")

    try:
        initialize_db()
        logger.info("Base de logs de consultas inicializada")
    except Exception as e:
        logger.error(f"Error inicializando base de logs: {e}")

    try:
        initialize_auth_db()
        # Crear usuario admin si no existe
        if not admin_exists() and ADMIN_EMAIL and ADMIN_PASSWORD:
            import uuid
            from datetime import datetime, timezone
            create_user(
                user_id=str(uuid.uuid4()),
                email=ADMIN_EMAIL,
                name="Administrador",
                company="Toxiro Digital",
                password_hash=hash_password(ADMIN_PASSWORD),
                role="admin",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=None,
            )
            logger.info(f"Usuario admin creado: {ADMIN_EMAIL}")
    except Exception as e:
        logger.error(f"Error inicializando auth: {e}")

    try:
        start_scheduler()
        logger.info("Scheduler iniciado")
    except Exception as e:
        logger.error(f"Error iniciando scheduler: {e}")

    logger.info("AgentIA Aduanas listo en http://0.0.0.0:8000")

    yield

    logger.info("Cerrando AgentIA Aduanas...")
    stop_scheduler()
    vector_store.close()
    logger.info("Aplicación cerrada correctamente")


# ------------------------------------------------------------------ #
# Aplicación FastAPI                                                   #
# ------------------------------------------------------------------ #
app = FastAPI(
    title="AgentIA Aduanas",
    description="Agente IA para consultas de normativa aduanera chilena",
    version="1.0.0",
    lifespan=lifespan,
)

# Auth router
app.include_router(auth_router)

# Billing router
app.include_router(billing_router)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ #
# Modelos Pydantic                                                     #
# ------------------------------------------------------------------ #
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="Consulta del usuario")
    filter: Literal["all", "normativa", "internos"] = Field(
        default="all",
        description="Filtro de colección: 'all', 'normativa' o 'internos'",
    )


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]]
    query_type: str
    total_chunks_retrieved: int


class DocumentInfo(BaseModel):
    doc_id: str
    title: str
    filename: str
    date: str
    url: str
    content_type: str
    source: str
    total_chunks: int


class ScraperStatusResponse(BaseModel):
    last_run: Optional[str]
    next_run: Optional[str]
    total_docs: int
    status: str
    is_running: bool
    scheduler_running: bool


# ------------------------------------------------------------------ #
# Rutas: Chat                                                          #
# ------------------------------------------------------------------ #
def _get_client_ip(request: Request) -> str:
    """Obtiene la IP real del cliente, considerando proxies."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else ""


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, x_client_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """
    Endpoint principal de consulta RAG.
    Recibe una pregunta y retorna la respuesta de Claude con fuentes.
    """
    import time
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="La consulta no puede estar vacía")

    # Verificar cuota (admins y demos pasan sin restricción de cuota)
    if current_user.get("role") not in ("admin", "demo"):
        from backend.billing.service import check_quota
        quota = check_quota(current_user)
        if not quota["allowed"]:
            raise HTTPException(status_code=429, detail=quota["reason"])

    client_ip = _get_client_ip(request)
    t0 = time.monotonic()
    try:
        result = await rag_engine.query(
            query=body.query,
            filter_collection=body.filter,
            user_id=current_user["id"],
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Registrar consulta (no bloqueante)
        try:
            log_query(
                query=body.query,
                filter_collection=body.filter,
                query_type=result.get("query_type", "general"),
                chunks_retrieved=result.get("total_chunks_retrieved", 0),
                duration_ms=duration_ms,
                client_id=x_client_id or "default",
                ip=client_ip,
            )
        except Exception:
            pass  # El log nunca debe interrumpir la respuesta

        # Descontar consulta del plan (async, no bloquea la respuesta)
        if current_user.get("role") not in ("admin", "demo"):
            from backend.billing.service import record_query
            asyncio.create_task(record_query(current_user["id"]))

        return ChatResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error en chat: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error interno al procesar la consulta. Intente nuevamente.",
        )


# ------------------------------------------------------------------ #
# Rutas: Documentos internos                                          #
# ------------------------------------------------------------------ #
@app.get("/api/documents")
async def list_documents(current_user: dict = Depends(get_current_user)):
    """Lista los documentos internos del usuario autenticado."""
    try:
        docs = vector_store.list_documents(COLLECTION_INTERNOS, user_id=current_user["id"])
        return {"documents": docs, "total": len(docs)}
    except Exception as e:
        logger.error(f"Error listando documentos: {e}")
        raise HTTPException(status_code=500, detail="Error al obtener la lista de documentos")


@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    content_type_override: Optional[str] = Form(None, alias="content_type"),
    date: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Sube e indexa un documento interno (PDF, DOCX o TXT).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No se recibió ningún archivo")

    # Validar extensión
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Formatos permitidos: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # Leer contenido
    content = await file.read()

    # Validar tamaño
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Archivo demasiado grande. Máximo permitido: {MAX_FILE_SIZE // (1024*1024)} MB",
        )

    if len(content) == 0:
        raise HTTPException(status_code=400, detail="El archivo está vacío")

    # Generar hash del contenido para deduplicación
    file_hash = hashlib.sha256(content).hexdigest()[:16]

    # Verificar si ya está indexado
    if vector_store.is_document_indexed(file_hash, id_type="doc_id", collection_name=COLLECTION_INTERNOS):
        raise HTTPException(
            status_code=409,
            detail="Este documento ya ha sido indexado anteriormente",
        )

    # Guardar archivo
    safe_filename = _sanitize_filename(file.filename)
    file_path = UPLOADS_DIR / safe_filename

    # Si ya existe un archivo con ese nombre, añadir el hash
    if file_path.exists():
        file_path = UPLOADS_DIR / f"{file_hash}_{safe_filename}"

    try:
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
    except Exception as e:
        logger.error(f"Error guardando archivo {safe_filename}: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar el archivo")

    # Procesar e indexar
    try:
        metadata = {
            "title": title or Path(file.filename).stem,
            "filename": file.filename,
            "content_type": content_type_override or _detect_doc_type(ext),
            "source": source or "Documento interno",
            "doc_id": file_hash,
            "url": "",
            "date": date or "",
            "user_id": current_user["id"],
        }

        loop = asyncio.get_running_loop()
        chunks = await loop.run_in_executor(
            None, document_processor.process_file, file_path, metadata
        )

        if not chunks:
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail="No se pudo extraer texto del documento. Verifique que no esté corrupto.",
            )

        added = await loop.run_in_executor(
            None, vector_store.add_documents, chunks, COLLECTION_INTERNOS
        )

        logger.info(f"Documento indexado: {file.filename} ({added} chunks)")
        return {
            "message": f"Documento '{file.filename}' indexado exitosamente",
            "doc_id": file_hash,
            "filename": file.filename,
            "chunks_created": added,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error indexando {file.filename}: {type(e).__name__}: {e}", exc_info=True)
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error al indexar el documento: {type(e).__name__}: {e}",
        )


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Elimina un documento interno por su doc_id."""
    if not doc_id or len(doc_id) > 64:
        raise HTTPException(status_code=400, detail="doc_id inválido")

    try:
        # Obtener info del documento antes de eliminar
        docs = vector_store.list_documents(COLLECTION_INTERNOS)
        doc_info = next((d for d in docs if d["doc_id"] == doc_id), None)

        deleted = vector_store.delete_document(doc_id, COLLECTION_INTERNOS)

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Documento con ID '{doc_id}' no encontrado",
            )

        # Eliminar archivo físico si existe
        if doc_info and doc_info.get("filename"):
            _try_delete_file(doc_info["filename"], doc_id)

        logger.info(f"Documento eliminado: {doc_id}")
        return {"message": f"Documento eliminado exitosamente", "doc_id": doc_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error eliminando documento {doc_id}: {e}")
        raise HTTPException(status_code=500, detail="Error al eliminar el documento")


# ------------------------------------------------------------------ #
# Rutas: Scraper                                                       #
# ------------------------------------------------------------------ #
@app.get("/api/scraper/status", response_model=ScraperStatusResponse)
async def scraper_status():
    """Retorna el estado del scraper y estadísticas de la base de datos."""
    try:
        status = get_status()
        stats = vector_store.get_stats()

        return ScraperStatusResponse(
            last_run=status.get("last_run"),
            next_run=status.get("next_run"),
            total_docs=stats.get("total", 0),
            status=status.get("last_status", "desconocido"),
            is_running=status.get("scraper_currently_running", False),
            scheduler_running=status.get("scheduler_running", False),
        )
    except Exception as e:
        logger.error(f"Error obteniendo estado del scraper: {e}")
        raise HTTPException(status_code=500, detail="Error al obtener el estado del scraper")


@app.post("/api/scraper/run")
async def trigger_scraper():
    """Dispara una ejecución manual del scraper."""
    status = get_status()
    if status.get("scraper_currently_running"):
        raise HTTPException(
            status_code=409,
            detail="El scraper ya está en ejecución. Espere a que termine.",
        )

    asyncio.create_task(_run_scraper_background())

    return {
        "message": "Scraping iniciado. El proceso se ejecuta en segundo plano.",
        "status": "iniciado",
    }


@app.post("/api/scraper/reset")
async def reset_normativa():
    """Limpia la colección de normativa y lanza un nuevo scraping desde cero."""
    from backend.indexer.vectorstore import COLLECTION_NORMATIVA
    status = get_status()
    if status.get("scraper_currently_running"):
        raise HTTPException(
            status_code=409,
            detail="El scraper ya está en ejecución. Espere a que termine.",
        )
    try:
        vector_store.clear_collection(COLLECTION_NORMATIVA)
        logger.info("Colección normativa_aduanera limpiada")
    except Exception as e:
        logger.error(f"Error limpiando colección: {e}")
        raise HTTPException(status_code=500, detail=f"Error al limpiar la colección: {e}")

    asyncio.create_task(_run_scraper_background())

    return {
        "message": "Base de normativa limpiada. Re-indexando desde cero en segundo plano.",
        "status": "reiniciado",
    }


async def _run_scraper_background():
    """Ejecuta el scraper en segundo plano."""
    try:
        await run_now()
    except Exception as e:
        logger.error(f"Error en scraping en background: {e}")


@app.get("/api/scraper/logs")
async def get_scraper_logs():
    """Retorna las últimas 50 líneas del log del scraper."""
    log_file = LOGS_DIR / "scraper.log"

    if not log_file.exists():
        return {"logs": [], "message": "No hay logs disponibles aún"}

    try:
        async with aiofiles.open(log_file, "r", encoding="utf-8") as f:
            content = await f.read()

        lines = content.splitlines()
        last_50 = lines[-50:] if len(lines) > 50 else lines

        return {"logs": last_50, "total_lines": len(lines)}
    except Exception as e:
        logger.error(f"Error leyendo logs: {e}")
        raise HTTPException(status_code=500, detail="Error al leer los logs")


# ------------------------------------------------------------------ #
# Rutas: Logs de consultas                                             #
# ------------------------------------------------------------------ #
@app.get("/api/admin/queries")
async def admin_queries(
    limit: int = 100,
    offset: int = 0,
    client_id: Optional[str] = None,
    ip: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Lista las consultas registradas con filtros opcionales."""
    try:
        return get_queries(
            limit=min(limit, 500),
            offset=offset,
            client_id=client_id,
            ip=ip,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as e:
        logger.error(f"Error leyendo logs de consultas: {e}")
        raise HTTPException(status_code=500, detail="Error al leer los logs")


@app.get("/api/admin/queries/summary")
async def admin_queries_summary(client_id: Optional[str] = None):
    """Resumen de uso: totales por día, tipo y cliente."""
    try:
        return get_summary(client_id=client_id)
    except Exception as e:
        logger.error(f"Error generando resumen de consultas: {e}")
        raise HTTPException(status_code=500, detail="Error al generar el resumen")


# ------------------------------------------------------------------ #
# Archivos estáticos y frontend                                        #
# ------------------------------------------------------------------ #
@app.get("/api/uploads/{filename}")
async def serve_upload(filename: str):
    """Sirve archivos subidos por el usuario."""
    # Intentar primero con el nombre saneado (igual que al guardar)
    safe_name = _sanitize_filename(filename)
    file_path = UPLOADS_DIR / safe_name
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    # Buscar con prefijo hash (e.g. abc123_nombre.pdf)
    for f in UPLOADS_DIR.iterdir():
        if f.is_file() and f.name.endswith("_" + safe_name):
            return FileResponse(str(f))
    raise HTTPException(status_code=404, detail="Archivo no encontrado")


if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


@app.get("/landing")
async def serve_landing():
    """Sirve la landing page de marketing."""
    landing_path = FRONTEND_DIR / "landing.html"
    if not landing_path.exists():
        return JSONResponse(status_code=404, content={"detail": "Landing page no encontrada"})
    return FileResponse(str(landing_path))


@app.get("/admin")
async def serve_admin():
    """Sirve el panel de administración."""
    admin_path = FRONTEND_DIR / "admin.html"
    if not admin_path.exists():
        return JSONResponse(status_code=404, content={"detail": "Panel admin no encontrado"})
    return FileResponse(str(admin_path))


@app.get("/")
async def serve_index():
    """Sirve la página principal del frontend."""
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend no encontrado. Ejecute desde la raíz del proyecto."},
        )
    return FileResponse(str(index_path))


@app.get("/health")
async def health_check():
    """Endpoint de verificación de salud del sistema."""
    try:
        stats = vector_store.get_stats()
        return {
            "status": "ok",
            "vectorstore": "conectado",
            "docs_normativa": stats.get("normativa_aduanera", 0),
            "docs_internos": stats.get("documentos_internos", 0),
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(e)},
        )


# ------------------------------------------------------------------ #
# Utilidades                                                           #
# ------------------------------------------------------------------ #
def _sanitize_filename(filename: str) -> str:
    """Sanitiza el nombre de archivo para almacenamiento seguro."""
    import re
    # Mantener solo caracteres seguros
    name = Path(filename).stem
    ext = Path(filename).suffix
    safe_name = re.sub(r"[^\w\s\-.]", "_", name)
    safe_name = re.sub(r"\s+", "_", safe_name)
    return f"{safe_name[:100]}{ext}"


def _detect_doc_type(ext: str) -> str:
    """Detecta el tipo de documento por extensión."""
    mapping = {
        ".pdf": "pdf",
        ".docx": "word",
        ".doc": "word",
        ".txt": "texto",
        ".png": "imagen",
        ".jpg": "imagen",
        ".jpeg": "imagen",
        ".tiff": "imagen",
        ".tif": "imagen",
        ".bmp": "imagen",
        ".webp": "imagen",
    }
    return mapping.get(ext.lower(), "documento")


def _try_delete_file(filename: str, doc_id: str):
    """Intenta eliminar el archivo físico del documento."""
    try:
        # Buscar por nombre exacto
        file_path = UPLOADS_DIR / _sanitize_filename(filename)
        if file_path.exists():
            file_path.unlink()
            return
        # Buscar con prefijo hash
        alt_path = UPLOADS_DIR / f"{doc_id}_{_sanitize_filename(filename)}"
        if alt_path.exists():
            alt_path.unlink()
    except Exception as e:
        logger.warning(f"No se pudo eliminar archivo físico: {e}")
