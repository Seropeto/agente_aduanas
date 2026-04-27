"""
Scraper para el Diario Oficial de la República de Chile.
URL base: https://www.diariooficial.interior.gob.cl/edicionelectronica/
Extrae: resoluciones y decretos de aduanas, SII y Hacienda publicados en los últimos días.
"""
import io
import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.diariooficial.interior.gob.cl"
EDITION_URL = f"{BASE_URL}/edicionelectronica/index.php"

KEYWORDS_RELEVANTES = [
    "aduana", "aduanas", "arancel",
    "importación", "importacion", "exportación", "exportacion",
    "zona franca", "derechos de aduana", "compendio de normas aduaneras",
]

INSTITUCIONES_RELEVANTES = [
    "aduanas", "hacienda", "sii", "impuestos internos",
    "servicio nacional de aduanas", "banco central",
]

DIAS_A_REVISAR = 14


class DiarioOficialScraper(BaseScraper):
    """
    Scraper para el Diario Oficial de Chile (edición electrónica).
    Navega por edición diaria y descarga PDFs de documentos de aduanas.
    """

    SOURCE_NAME = "Diario Oficial de la República de Chile"

    async def scrape(self) -> list[dict[str, Any]]:
        """Revisa las últimas 2 semanas de ediciones del Diario Oficial."""
        documents = []

        async with self as scraper:
            for dias_atras in range(DIAS_A_REVISAR):
                fecha = date.today() - timedelta(days=dias_atras)

                # El Diario Oficial no publica los domingos
                if fecha.weekday() == 6:
                    continue

                try:
                    docs_del_dia = await scraper._scrape_edition(fecha)
                    documents.extend(docs_del_dia)
                except Exception as e:
                    logger.error(f"Error scrapeando edición {fecha}: {e}")

        # Deduplicar por URL
        seen_urls: set[str] = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total documentos únicos del Diario Oficial: {len(unique_docs)}")
        return unique_docs

    async def _scrape_edition(self, fecha: date) -> list[dict[str, Any]]:
        """Parsea la edición electrónica de una fecha específica."""
        date_str = fecha.strftime("%d-%m-%Y")
        url = f"{EDITION_URL}?date={date_str}"

        html = await self.fetch(url)
        if not html:
            logger.debug(f"Sin edición disponible para {date_str}")
            return []

        soup = self.parse_html(html, BASE_URL)

        # El sitio muestra este mensaje cuando no hay publicaciones
        sin_pub = soup.find(string=lambda t: t and "no existen publicaciones" in t.lower() if t else False)
        if sin_pub:
            logger.debug(f"Sin publicaciones para {date_str}")
            return []

        documents = []

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not href.lower().endswith(".pdf"):
                continue

            full_url = urljoin(BASE_URL, href)
            if "/publicaciones/" not in full_url:
                continue

            # Construir contexto de texto alrededor del enlace
            title = self.clean_text(link.get_text()) or ""
            parent_text = ""
            for parent in [link.find_parent("p"), link.find_parent("li"),
                           link.find_parent("tr"), link.find_parent("div")]:
                if parent:
                    parent_text = self.clean_text(parent.get_text())
                    break

            context = f"{title} {parent_text}".lower()

            if not self._es_relevante(context):
                continue

            doc_type = self._detect_doc_type(context)
            doc_title = title or parent_text[:200] or f"Publicación Diario Oficial {date_str}"

            logger.info(f"Documento relevante: {doc_title[:80]}")

            content = await self._extract_pdf_content(full_url, doc_title)
            if not content or len(content.strip()) < 50:
                content = parent_text or doc_title

            documents.append({
                "title": doc_title[:300],
                "url": full_url,
                "date": fecha.strftime("%Y-%m-%d"),
                "content": content[:8000],
                "content_type": doc_type,
                "source": self.SOURCE_NAME,
            })

        return documents

    async def _extract_pdf_content(self, url: str, fallback: str) -> str:
        """Descarga un PDF y extrae su texto con pdfplumber."""
        pdf_bytes = await self.fetch_bytes(url)
        if not pdf_bytes:
            return fallback

        try:
            import pdfplumber

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = Path(tmp.name)

            try:
                with pdfplumber.open(str(tmp_path)) as pdf:
                    texts = []
                    for page in pdf.pages[:10]:
                        text = page.extract_text() or ""
                        if text.strip():
                            texts.append(text)
                    return "\n\n".join(texts)
            finally:
                tmp_path.unlink(missing_ok=True)

        except ImportError:
            logger.warning("pdfplumber no disponible, usando PyPDF2")
            return self._extract_pdf_pypdf2(pdf_bytes, fallback)
        except Exception as e:
            logger.warning(f"Error extrayendo PDF {url}: {e}")
            return fallback

    def _extract_pdf_pypdf2(self, pdf_bytes: bytes, fallback: str) -> str:
        """Extracción de respaldo usando PyPDF2."""
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            texts = [p.extract_text() or "" for p in reader.pages[:10]]
            return "\n\n".join(t for t in texts if t.strip())
        except Exception as e:
            logger.warning(f"Error con PyPDF2: {e}")
            return fallback

    def _es_relevante(self, texto: str) -> bool:
        """Verifica si el texto contiene palabras clave de aduanas."""
        return any(kw in texto for kw in KEYWORDS_RELEVANTES + INSTITUCIONES_RELEVANTES)

    def _detect_doc_type(self, texto: str) -> str:
        """Detecta el tipo de documento."""
        if "circular" in texto:
            return "circular"
        if "resolución" in texto or "resolucion" in texto:
            return "resolucion"
        if "decreto" in texto:
            return "decreto"
        if "ley " in texto:
            return "ley"
        if "arancel" in texto:
            return "arancel"
        return "publicacion"
