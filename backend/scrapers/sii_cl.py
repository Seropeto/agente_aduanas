"""
Scraper para el Servicio de Impuestos Internos de Chile.
URL base: https://www.sii.cl
Extrae: normativa tributaria sobre IVA en importaciones, derechos aduaneros,
        tratamiento tributario de bienes importados.
"""
import logging
from typing import Any
from urllib.parse import urljoin, urlparse

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sii.cl"

# Secciones específicas de interés en el SII para aduanas
SII_PAGES = [
    {
        "url": f"{BASE_URL}/normativa_legislacion/circulares/",
        "content_type": "circular",
        "label": "Circulares SII",
        "keywords": ["importación", "exportación", "aduanas", "iva importaciones"],
    },
    {
        "url": f"{BASE_URL}/normativa_legislacion/resoluciones/",
        "content_type": "resolucion",
        "label": "Resoluciones SII",
        "keywords": ["importación", "aduana", "zona franca"],
    },
    {
        "url": f"{BASE_URL}/ayudas_y_formatos/importar_exportar/index.html",
        "content_type": "procedimiento",
        "label": "Importar/Exportar SII",
        "keywords": [],
    },
    {
        "url": f"{BASE_URL}/impuestos_mercado_capitales/iva/index.html",
        "content_type": "normativa",
        "label": "IVA - SII",
        "keywords": ["importación"],
    },
    {
        "url": f"{BASE_URL}/normativa_legislacion/legislacion/",
        "content_type": "ley",
        "label": "Legislación SII",
        "keywords": ["aduana", "importación", "exportación"],
    },
]

# Páginas específicas sobre IVA e importaciones
SPECIFIC_PAGES = [
    {
        "url": f"{BASE_URL}/ayudas_y_formatos/importar_exportar/iva_importaciones.html",
        "title": "IVA en Importaciones - SII Chile",
        "content_type": "normativa",
        "date": "",
    },
    {
        "url": f"{BASE_URL}/ayudas_y_formatos/importar_exportar/derechos_importacion.html",
        "title": "Derechos de Importación - SII Chile",
        "content_type": "normativa",
        "date": "",
    },
    {
        "url": f"{BASE_URL}/impuestos_mercado_capitales/otros_impuestos/impuesto_adicional/index.html",
        "title": "Impuesto Adicional a Bienes Importados - SII Chile",
        "content_type": "normativa",
        "date": "",
    },
]


class SIIScraper(BaseScraper):
    """
    Scraper para www.sii.cl.
    Extrae normativa tributaria relevante para agencias de aduanas.
    """

    SOURCE_NAME = "Servicio de Impuestos Internos (SII)"

    async def scrape(self) -> list[dict[str, Any]]:
        """Ejecuta el scraping del SII."""
        documents = []

        async with self as scraper:
            # Páginas específicas
            for page in SPECIFIC_PAGES:
                try:
                    doc = await scraper._scrape_page(page)
                    if doc:
                        documents.append(doc)
                except Exception as e:
                    logger.error(f"Error scraping {page['url']}: {e}")

            # Secciones generales con filtrado por keywords
            for section in SII_PAGES:
                try:
                    section_docs = await scraper._scrape_section(section)
                    documents.extend(section_docs)
                    logger.info(
                        f"Sección SII '{section['label']}': {len(section_docs)} documentos"
                    )
                except Exception as e:
                    logger.error(f"Error en sección SII '{section['label']}': {e}")

        # Deduplicar por URL
        seen_urls = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total documentos únicos del SII: {len(unique_docs)}")
        return unique_docs

    async def _scrape_page(self, page_info: dict) -> dict[str, Any] | None:
        """Descarga una página específica del SII."""
        html = await self.fetch(page_info["url"])
        if not html:
            # No indexar páginas que no responden — evita guardar URLs muertas
            logger.warning(f"Página SII no disponible, se omite: {page_info['url']}")
            return None

        soup = self.parse_html(html, page_info["url"])

        # Eliminar elementos no relevantes
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()

        # Intentar obtener el contenido principal
        content_el = (
            soup.find("div", id="contenido")
            or soup.find("div", id="content")
            or soup.find("main")
            or soup.find("article")
            or soup.find("div", class_="contenido")
            or soup.find("div", class_="content")
            or soup.body
        )

        content_text = ""
        if content_el:
            content_text = self.clean_text(content_el.get_text(separator=" "))

        if not content_text:
            content_text = self.clean_text(soup.get_text(separator=" "))

        # Obtener título
        title = page_info["title"]
        title_el = soup.find("h1") or soup.find("title")
        if title_el:
            extracted = self.clean_text(title_el.get_text())
            if extracted and len(extracted) > 5:
                title = extracted

        date = page_info.get("date", "") or self.extract_date_from_text(content_text[:500])

        return {
            "title": title[:300],
            "url": page_info["url"],
            "date": date,
            "content": content_text[:8000],
            "content_type": page_info.get("content_type", "normativa"),
            "source": self.SOURCE_NAME,
        }

    async def _scrape_section(self, section: dict) -> list[dict[str, Any]]:
        """Scrape una sección del SII y filtra por keywords."""
        documents = []
        keywords = section.get("keywords", [])

        html = await self.fetch(section["url"])
        if not html:
            return documents

        soup = self.parse_html(html, BASE_URL)

        # Buscar todos los enlaces en la página
        links = soup.find_all("a", href=True)

        for link in links[:100]:  # Limitar a 100 links por sección
            href = link.get("href", "")
            title = self.clean_text(link.get_text())

            if not title or len(title) < 8:
                continue

            # Aplicar filtro de keywords si existe
            if keywords:
                title_lower = title.lower()
                href_lower = href.lower()
                if not any(
                    kw in title_lower or kw in href_lower
                    for kw in keywords
                ):
                    continue

            full_url = urljoin(BASE_URL, href)

            # Validar que sea del dominio SII o relacionado
            parsed = urlparse(full_url)
            if not ("sii.cl" in parsed.netloc):
                continue

            # Obtener fecha del contexto
            parent = link.find_parent("tr") or link.find_parent("li") or link.find_parent("div")
            date_text = parent.get_text() if parent else title
            date = self.extract_date_from_text(date_text)

            # Para circulares y resoluciones, intentar obtener el contenido
            content = title
            if section["content_type"] in ("circular", "resolucion") and len(documents) < 10:
                content = await self._fetch_content(full_url, title)

            documents.append({
                "title": title[:300],
                "url": full_url,
                "date": date,
                "content": content[:5000],
                "content_type": section["content_type"],
                "source": self.SOURCE_NAME,
            })

            if len(documents) >= 20:
                break

        return documents

    async def _fetch_content(self, url: str, fallback: str) -> str:
        """Obtiene el contenido de una página del SII."""
        if url.lower().endswith(".pdf"):
            return f"Documento PDF: {fallback}"

        html = await self.fetch(url)
        if not html:
            return fallback

        soup = self.parse_html(html, url)
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        content_el = (
            soup.find("div", id="contenido")
            or soup.find("div", id="content")
            or soup.find("main")
            or soup.body
        )

        if content_el:
            return self.clean_text(content_el.get_text(separator=" "))[:5000]

        return self.clean_text(soup.get_text(separator=" "))[:5000]
