"""
cron_update_laws.py — Ingesta autónoma de Normativa Oficial (Ticket 004)

Proceso autónomo (ejecutable por cron / consola) que ingesta la normativa oficial
chilena en la colección `normativa_aduanera` de ChromaDB, con chunking ESTRUCTURAL
por artículo y cabecera jerárquica inyectada en cada fragmento.

Esta es la ÚNICA vía autorizada para poblar la normativa oficial. La carga manual
vía API fue eliminada (ver deprecación en backend/main.py).

Fuentes:
  - BCN / Ley Chile: API/portal oficial para leyes macro (DFL 30, DL 825, etc.).
  - Aduanas / SII: scrapers de circulares y resoluciones.

Uso:
  python cron_update_laws.py --file ruta/al/dfl30.txt --law-name "DFL 30" --source BCN
  python cron_update_laws.py --id-norma 179549 --law-name "DFL 30 - Ordenanza de Aduanas"
  python cron_update_laws.py --file ruta.txt --law-name "DFL 30" --dry-run   # solo parsea

Alertas:
  Si el parser no encuentra artículos (posible cambio de estructura en el portal
  de origen), se envía una alerta a ALERT_WEBHOOK_URL (si está configurada).
"""
import argparse
import hashlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cron_update_laws")

# ── Expresiones regulares de estructura legal chilena ─────────────────────────
# Límite estricto de artículo: "Artículo 40", "Art. 40°.-", "ARTÍCULO 1 bis"
ARTICULO_RE = re.compile(
    r"(?im)^\s*(?:Art[íi]culo|ART[ÍI]CULO|Art\.)\s+(\d+)\s*(?:°|º)?\s*(bis|ter|qu[áa]ter)?\s*[\.\-]*"
)
TITULO_RE = re.compile(r"(?im)^\s*(T[ÍI]TULO\s+[IVXLCDM\d]+[^\n]*)")
CAPITULO_RE = re.compile(r"(?im)^\s*(CAP[ÍI]TULO\s+[IVXLCDM\d]+[^\n]*)")
PARRAFO_RE = re.compile(r"(?im)^\s*(P[ÁA]RRAFO\s+[IVXLCDM\d]+[^\n]*)")
# Unidad alternativa para el Arancel: Reglas Generales Interpretativas del S.A.
REGLA_RE = re.compile(r"(?im)^\s*(?:Regla|REGLA)\s+(\d+)\s*[\.\-]*")

# ── Registro de cuerpos legales oficiales (BCN / Ley Chile) ───────────────────
# idNorma son los identificadores oficiales del portal leychile.cl. Verificar en
# https://www.leychile.cl antes de un seed productivo (los IDs pueden cambiar).
LAWS_REGISTRY = [
    {"id_norma": "179549", "law_name": "DFL 30 - Ordenanza de Aduanas", "source": "BCN"},
    {"id_norma": "6368",   "law_name": "DL 825 - Ley sobre Impuesto a las Ventas y Servicios (IVA)", "source": "BCN"},
    {"id_norma": "235492", "law_name": "Arancel Aduanero de Chile", "source": "BCN"},
]

# NOTA DE FUENTES (Ticket 004-B):
#  - Reglas Generales de Interpretación del S.A. (RGI 1-6) y partidas 9026/9032:
#    forman parte del Arancel Aduanero (decreto). Si el Arancel no expone el
#    articulado por la API, proveer el texto oficial via --file.
#  - Textos de TLC (Chile-EE.UU., Chile-UE, India): la fuente oficial es SUBREI,
#    que BLOQUEA el scraping automatizado con HTTP 403. No se pueden auto-descargar;
#    proveer por --file o fuente alterna. El cron alerta si la descarga falla.

# ── Fixtures legales offline (data/seed_laws/) para --seed-local ───────────────
# Texto curado de los artículos clave para poblar ChromaDB de forma determinista,
# sin depender de la descarga viva de BCN (frágil/bloqueada). Cada fixture se
# fragmenta por su unidad estructural (artículo o regla).
SEED_LOCAL_DIR = Path(__file__).parent / "data" / "seed_laws"
SEED_LOCAL_MANIFEST = [
    {"file": "dl825_iva_importaciones.txt", "law_name": "DL 825 - Ley sobre Impuesto a las Ventas y Servicios", "source": "BCN", "unit": "articulo"},
    {"file": "dfl30_ordenanza.txt",         "law_name": "DFL 30 - Ordenanza de Aduanas", "source": "BCN", "unit": "articulo"},
    {"file": "arancel_rgi.txt",             "law_name": "Arancel Aduanero - Reglas Generales Interpretativas del S.A.", "source": "Aduanas", "unit": "regla"},
    {"file": "partidas_sa.txt",             "law_name": "Arancel Aduanero - Partidas 90.26 y 90.32", "source": "Aduanas", "unit": "articulo"},
]


def _structural_markers(text: str) -> list[tuple[int, str]]:
    """Devuelve [(posición, etiqueta)] de Títulos/Capítulos/Párrafos, ordenados."""
    markers: list[tuple[int, str]] = []
    for rx in (TITULO_RE, CAPITULO_RE, PARRAFO_RE):
        for m in rx.finditer(text):
            markers.append((m.start(), re.sub(r"\s+", " ", m.group(1)).strip()))
    markers.sort(key=lambda x: x[0])
    return markers


def _hierarchy_at(markers: list[tuple[int, str]], pos: int) -> str:
    """Última jerarquía (Título/Capítulo/Párrafo) declarada antes de `pos`."""
    label = ""
    for mpos, mlabel in markers:
        if mpos <= pos:
            label = mlabel
        else:
            break
    return label


def parse_articles(text: str, law_name: str, source: str = "BCN",
                   unit_re: "re.Pattern | None" = None,
                   unit_label: str = "Artículo") -> list[dict[str, Any]]:
    """
    Fragmenta el texto legal por unidad estructural (no por caracteres/tokens ciegos).
    Por defecto la unidad es el ARTÍCULO; con unit_re=REGLA_RE fragmenta por Regla
    (Arancel / Reglas Generales Interpretativas del S.A.).

    Cada chunk recibe una cabecera jerárquica inyectada en el TEXTO:
      "[Origen: BCN - DFL 30, Título I, Artículo 40] <cuerpo del artículo>"

    Returns:
        Lista de chunks {text, metadata}. Vacía si no se detecta ninguna unidad
        (señal de cambio de estructura → el caller debe alertar).
    """
    rx = unit_re or ARTICULO_RE
    markers = _structural_markers(text)
    matches = list(rx.finditer(text))
    if not matches:
        return []

    law_doc_id = hashlib.sha256(f"{source}:{law_name}".encode("utf-8")).hexdigest()[:16]
    chunks: list[dict[str, Any]] = []

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        num = m.group(1)
        # group(2) (sufijo bis/ter) solo existe en ARTICULO_RE
        sufijo = (m.group(2) or "").strip() if rx.groups >= 2 else ""
        articulo_id = f"{num} {sufijo}".strip()

        jerarquia = _hierarchy_at(markers, start)
        cuerpo = text[start:end].strip()

        partes = [f"Origen: {source} - {law_name}"]
        if jerarquia:
            partes.append(jerarquia)
        partes.append(f"{unit_label} {articulo_id}")
        header = "[" + ", ".join(partes) + "]"

        chunk_text = f"{header} {cuerpo}"
        chunks.append({
            "text": chunk_text,
            "metadata": {
                "title": law_name,
                "source": source,
                "content_type": "ley",
                "doc_id": law_doc_id,
                "chunk_index": i,
                "total_chunks": len(matches),
                "articulo": articulo_id,
                "jerarquia": jerarquia,
                "tipo_documento": "ley_estructural",
                "url": "",
                "date": "",
            },
        })

    return chunks


# ── Alertas de resiliencia ────────────────────────────────────────────────────

def send_alert(message: str) -> None:
    """
    Envía una alerta a ALERT_WEBHOOK_URL si está configurada (webhook de dev).
    Siempre registra en logs. No lanza excepción (la alerta no debe romper el cron).
    """
    logger.error(f"[ALERTA] {message}")
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        import httpx
        httpx.post(url, json={"text": f"[AgentIA cron_update_laws] {message}"}, timeout=10)
        logger.info("[ALERTA] webhook notificado")
    except Exception as e:
        logger.error(f"[ALERTA] fallo enviando webhook: {e}")


# ── Fuentes de datos ──────────────────────────────────────────────────────────

def fetch_law_from_bcn(id_norma: str) -> str:
    """
    Descarga el texto de una ley desde el portal de Ley Chile (BCN).
    Usa el servicio de obtención de norma. Devuelve "" si falla.
    """
    import httpx
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}"
    headers = {"User-Agent": "Mozilla/5.0 (AgentIA cron_update_laws)"}
    try:
        r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True, verify=False)
        r.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        return soup.get_text("\n")
    except Exception as e:
        logger.error(f"Error descargando idNorma={id_norma} desde BCN: {e}")
        return ""


def scrape_aduana_circulares(vector_store=None, dry_run: bool = False) -> dict:
    """
    Captura circulares de clasificación arancelaria del Servicio Nacional de Aduanas
    reutilizando el AduanaScraper estable del backend. Si el scraper no devuelve nada
    (cambio de selectores/estructura del portal externo), dispara una ALERTA real.
    """
    import asyncio as _asyncio
    from backend.scrapers import AduanaScraper

    try:
        scraper = AduanaScraper()
        docs = _asyncio.run(scraper.scrape())
    except Exception as e:
        send_alert(f"Scraper de Aduanas falló (posible cambio de interfaz externa): {e}")
        return {"source": "Aduanas", "documents": 0, "indexed": 0}

    if not docs:
        send_alert(
            "Scraper de Aduanas devolvió 0 circulares. Posible cambio de selectores "
            "o estructura en el portal del Servicio Nacional de Aduanas."
        )
        return {"source": "Aduanas", "documents": 0, "indexed": 0}

    # Fragmentar cada circular por artículo cuando aplique; si no hay artículos,
    # se indexa el documento completo con su cabecera de origen.
    indexed = 0
    if not dry_run:
        from backend.indexer.vectorstore import VectorStore, COLLECTION_NORMATIVA
        if vector_store is None:
            vector_store = VectorStore()
            vector_store.initialize()
        for d in docs:
            chunks = parse_articles(d.get("content", ""), d.get("title", "Circular Aduanas"),
                                    source="Aduanas")
            if not chunks:
                # Documento sin articulado: indexar con cabecera mínima de origen
                header = f"[Origen: Aduanas - {d.get('title', 'Circular')}]"
                chunks = [{
                    "text": f"{header} {d.get('content', '')}",
                    "metadata": {
                        "title": d.get("title", "Circular Aduanas"),
                        "source": "Aduanas", "content_type": d.get("content_type", "circular"),
                        "doc_id": hashlib.sha256(d.get("url", d.get("title", "")).encode()).hexdigest()[:16],
                        "chunk_index": 0, "total_chunks": 1,
                        "url": d.get("url", ""), "date": d.get("date", ""),
                        "tipo_documento": "circular",
                    },
                }]
            indexed += ingest_chunks(chunks, vector_store)

    logger.info(f"Aduanas: {len(docs)} circulares, {indexed} chunks indexados")
    return {"source": "Aduanas", "documents": len(docs), "indexed": indexed}


def run_seed_laws(vector_store=None, dry_run: bool = False) -> list[dict]:
    """
    Descarga e indexa todos los cuerpos legales de LAWS_REGISTRY desde BCN en vivo.
    Cada fallo de descarga/parseo dispara una alerta pero no detiene el resto.
    """
    from backend.indexer.vectorstore import VectorStore
    if vector_store is None and not dry_run:
        vector_store = VectorStore()
        vector_store.initialize()

    resultados = []
    for ley in LAWS_REGISTRY:
        logger.info(f"Descargando {ley['law_name']} (idNorma={ley['id_norma']}) desde BCN...")
        text = fetch_law_from_bcn(ley["id_norma"])
        if not text:
            send_alert(f"Descarga vacía: {ley['law_name']} (idNorma={ley['id_norma']}). "
                       "Fuente caída o cambio de API de Ley Chile.")
            resultados.append({"law_name": ley["law_name"], "articulos": 0, "indexed": 0})
            continue
        chunks = parse_articles(text, ley["law_name"], ley["source"])
        if not chunks:
            send_alert(f"Sin artículos detectados en {ley['law_name']}. "
                       "Posible cambio de estructura del XML de BCN.")
            resultados.append({"law_name": ley["law_name"], "articulos": 0, "indexed": 0})
            continue
        indexed = 0 if dry_run else ingest_chunks(chunks, vector_store)
        logger.info(f"{ley['law_name']}: {len(chunks)} artículos, {indexed} indexados")
        resultados.append({"law_name": ley["law_name"], "articulos": len(chunks), "indexed": indexed})

    return resultados


def run_seed_local(vector_store=None, dry_run: bool = False) -> list[dict]:
    """
    Indexa los fixtures legales offline de data/seed_laws/ en ChromaDB.
    Población DETERMINISTA (sin red): garantiza que DFL 30, DL 825, RGI y partidas
    queden vectorizados en la base local. Cada fixture se fragmenta por su unidad.
    """
    from backend.indexer.vectorstore import VectorStore
    if vector_store is None and not dry_run:
        vector_store = VectorStore()
        vector_store.initialize()

    resultados = []
    for item in SEED_LOCAL_MANIFEST:
        path = SEED_LOCAL_DIR / item["file"]
        if not path.exists():
            send_alert(f"Fixture local ausente: {path}")
            resultados.append({"law_name": item["law_name"], "articulos": 0, "indexed": 0})
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        unit_re = REGLA_RE if item["unit"] == "regla" else None
        unit_label = "Regla" if item["unit"] == "regla" else "Artículo"
        chunks = parse_articles(text, item["law_name"], item["source"],
                                unit_re=unit_re, unit_label=unit_label)
        if not chunks:
            send_alert(f"Sin unidades detectadas en fixture {item['file']}")
            resultados.append({"law_name": item["law_name"], "articulos": 0, "indexed": 0})
            continue
        indexed = 0 if dry_run else ingest_chunks(chunks, vector_store)
        logger.info(f"{item['law_name']}: {len(chunks)} unidades, {indexed} indexados")
        resultados.append({
            "law_name": item["law_name"], "articulos": len(chunks),
            "indexed": indexed, "doc_id": chunks[0]["metadata"]["doc_id"],
        })

    return resultados


# ── Ingesta a PostgreSQL (pgvector, dominio 'normative') ──────────────────────

def ingest_chunks(chunks: list[dict[str, Any]], vector_store=None) -> int:
    """
    Indexa los chunks en el dominio de normativa oficial (pgvector).

    Bridge sync→async: abre un pool de PostgreSQL propio para esta invocación,
    embebe con OpenAI e inserta, y lo cierra. Determinista aunque queden globals
    de un asyncio.run previo (init_pool recrea el pool en el loop actual).
    """
    import asyncio as _asyncio
    from backend.indexer.vectorstore import VectorStore, COLLECTION_NORMATIVA
    from backend.database import init_pool, close_pool, is_pg_enabled

    async def _run() -> int:
        await init_pool()
        if not is_pg_enabled():
            logger.error("PostgreSQL no disponible — no se pueden indexar los chunks")
            return 0
        try:
            vs = VectorStore()
            return await vs.aadd_documents(chunks, COLLECTION_NORMATIVA)
        finally:
            await close_pool()

    return _asyncio.run(_run())


def ingest_file(
    path: str | Path,
    law_name: str,
    source: str = "BCN",
    vector_store=None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Lee un archivo de texto legal, lo fragmenta por artículo e indexa.
    Si no se detectan artículos, dispara una alerta y NO indexa.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    chunks = parse_articles(text, law_name, source)

    if not chunks:
        send_alert(
            f"Parser no detectó artículos en '{law_name}' ({path}). "
            "Posible cambio de estructura en la fuente oficial."
        )
        return {"law_name": law_name, "articulos": 0, "indexed": 0, "doc_id": None}

    doc_id = chunks[0]["metadata"]["doc_id"]
    logger.info(f"'{law_name}': {len(chunks)} articulos detectados (doc_id={doc_id})")

    indexed = 0
    if not dry_run:
        indexed = ingest_chunks(chunks, vector_store)
        logger.info(f"'{law_name}': {indexed} chunks indexados en normativa_aduanera")
    else:
        logger.info(f"[dry-run] no se indexa; muestra de cabecera: {chunks[0]['text'][:90]}")

    return {
        "law_name": law_name,
        "articulos": len(chunks),
        "indexed": indexed,
        "doc_id": doc_id,
        "headers": [c["text"][: c["text"].index("]") + 1] for c in chunks],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingesta autónoma de normativa oficial (Ticket 004)")
    parser.add_argument("--file", help="Ruta a un archivo de texto legal local")
    parser.add_argument("--id-norma", help="idNorma de Ley Chile/BCN para descarga en vivo")
    parser.add_argument("--law-name", default="Ley sin nombre", help="Nombre de la ley (para la cabecera)")
    parser.add_argument("--source", default="BCN", help="Fuente: BCN | Aduanas | SII")
    parser.add_argument("--seed-laws", action="store_true", help="Descarga e indexa todo LAWS_REGISTRY desde BCN (red)")
    parser.add_argument("--seed-local", action="store_true", help="Indexa los fixtures offline de data/seed_laws/ (sin red)")
    parser.add_argument("--aduana", action="store_true", help="Scrapea circulares del Servicio Nacional de Aduanas")
    parser.add_argument("--dry-run", action="store_true", help="Solo parsea, no indexa en ChromaDB")
    args = parser.parse_args(argv)

    try:
        if args.seed_local:
            resultados = run_seed_local(dry_run=args.dry_run)
            total = sum(r["indexed"] for r in resultados)
            print(f"RESULTADO seed-local: {len(resultados)} cuerpos legales, {total} chunks indexados")
            return 0
        elif args.seed_laws:
            resultados = run_seed_laws(dry_run=args.dry_run)
            total = sum(r["indexed"] for r in resultados)
            print(f"RESULTADO seed-laws: {len(resultados)} leyes procesadas, {total} chunks indexados")
            return 0
        elif args.aduana:
            r = scrape_aduana_circulares(dry_run=args.dry_run)
            print(f"RESULTADO aduana: {r['documents']} circulares, {r['indexed']} chunks indexados")
            return 0
        elif args.file:
            result = ingest_file(args.file, args.law_name, args.source, dry_run=args.dry_run)
        elif args.id_norma:
            text = fetch_law_from_bcn(args.id_norma)
            if not text:
                send_alert(f"Descarga vacia para idNorma={args.id_norma} (fuente caida o cambio de API)")
                return 2
            tmp = Path(f"bcn_{args.id_norma}.tmp.txt")
            try:
                tmp.write_text(text, encoding="utf-8")
                result = ingest_file(tmp, args.law_name, args.source, dry_run=args.dry_run)
            finally:
                tmp.unlink(missing_ok=True)
        else:
            parser.error("Debe especificar --file o --id-norma")
            return 1

        print(f"RESULTADO: {result['articulos']} articulo(s), {result['indexed']} chunk(s) indexado(s) "
              f"(doc_id={result['doc_id']})")
        return 0

    except Exception as e:
        send_alert(f"cron_update_laws fallo: {type(e).__name__}: {e}")
        logger.exception("Fallo en la ejecucion del cron")
        return 3


if __name__ == "__main__":
    sys.exit(main())
