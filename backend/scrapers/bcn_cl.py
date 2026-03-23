"""
Scraper para la Biblioteca del Congreso Nacional de Chile.
URL base: https://www.bcn.cl
Extrae: Ordenanza de Aduanas (DFL N°30), leyes relacionadas con comercio exterior.
"""
import logging
from typing import Any
from urllib.parse import urljoin, urlencode, urlparse

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bcn.cl"
LEYCHILE_URL = "https://www.leychile.cl"

# Leyes y decretos de interés aduanero
NORMAS_INTERES = [
    {
        "url": f"{LEYCHILE_URL}/Navegar?idNorma=179549",
        "title": "Ordenanza de Aduanas - DFL N°30 de 2005",
        "content_type": "ley",
        "date": "2005-12-02",
    },
    {
        "url": f"{LEYCHILE_URL}/Navegar?idNorma=248892",
        "title": "Ley N°18.164 - Modifica Ordenanza Aduanas",
        "content_type": "ley",
        "date": "1982-08-18",
    },
    {
        "url": f"{LEYCHILE_URL}/Navegar?idNorma=29066",
        "title": "DL N°825 - Ley sobre Impuesto a las Ventas y Servicios (IVA importaciones)",
        "content_type": "ley",
        "date": "1974-12-31",
    },
    {
        "url": f"{LEYCHILE_URL}/Navegar?idNorma=30565",
        "title": "DL N°3.001 - Procedimientos aduaneros",
        "content_type": "decreto",
        "date": "1979-06-12",
    },
    {
        "url": f"{LEYCHILE_URL}/Navegar?idNorma=1004286",
        "title": "Ley N°21.081 - Acuerdo sobre facilitación del comercio OMC",
        "content_type": "ley",
        "date": "2018-08-16",
    },
]

# Búsquedas temáticas en BCN
BCN_SEARCH_QUERIES = [
    "ordenanza aduanas",
    "arancel aduanero chile",
    "derechos aduaneros",
    "importación exportación",
    "zona franca",
]


class BCNScraper(BaseScraper):
    """
    Scraper para la Biblioteca del Congreso Nacional y LeyChile.
    Extrae normativa legal relacionada con aduanas y comercio exterior.
    """

    SOURCE_NAME = "Biblioteca del Congreso Nacional de Chile"

    async def scrape(self) -> list[dict[str, Any]]:
        """Ejecuta el scraping de la BCN."""
        documents = []

        async with self as scraper:
            # Scrape normas específicas de interés
            for norma in NORMAS_INTERES:
                try:
                    doc = await scraper._scrape_norma(norma)
                    if doc:
                        documents.append(doc)
                        logger.info(f"Obtenida norma: {norma['title']}")
                except Exception as e:
                    logger.error(f"Error scraping norma {norma['title']}: {e}")

            # Búsqueda general en BCN
            for query in BCN_SEARCH_QUERIES:
                try:
                    search_docs = await scraper._search_bcn(query)
                    documents.extend(search_docs)
                except Exception as e:
                    logger.error(f"Error en búsqueda BCN '{query}': {e}")

        # Deduplicar por URL
        seen_urls = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total documentos únicos de BCN: {len(unique_docs)}")
        return unique_docs

    async def _scrape_norma(self, norma_info: dict) -> dict[str, Any] | None:
        """Descarga y extrae el contenido de una norma legal específica."""
        html = await self.fetch(norma_info["url"])
        if not html:
            # No indexar normas que no responden — evita guardar URLs muertas
            logger.warning(f"Norma no disponible, se omite: {norma_info['url']}")
            return None

        soup = self.parse_html(html, norma_info["url"])

        # Eliminar elementos de navegación
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Buscar el cuerpo de la ley
        content_selectors = [
            ("div", {"id": "contenido"}),
            ("div", {"id": "norma"}),
            ("div", {"class": "norma-texto"}),
            ("article", {}),
            ("main", {}),
            ("div", {"id": "main-content"}),
        ]

        content_text = ""
        for tag_name, attrs in content_selectors:
            el = soup.find(tag_name, attrs)
            if el:
                content_text = self.clean_text(el.get_text(separator=" "))
                if len(content_text) > 200:
                    break

        if not content_text:
            content_text = self.clean_text(soup.get_text(separator=" "))

        # Extraer título desde la página
        title_el = (
            soup.find("h1")
            or soup.find("h2")
            or soup.find("title")
        )
        title = norma_info["title"]
        if title_el:
            extracted_title = self.clean_text(title_el.get_text())
            if extracted_title and len(extracted_title) > 5:
                title = extracted_title

        # Extraer fecha
        date = norma_info.get("date", "")
        if not date:
            date = self.extract_date_from_text(content_text[:500])

        return {
            "title": title[:300],
            "url": norma_info["url"],
            "date": date,
            "content": content_text[:8000],
            "content_type": norma_info.get("content_type", "ley"),
            "source": self.SOURCE_NAME,
        }

    async def _search_bcn(self, query: str) -> list[dict[str, Any]]:
        """Realiza búsqueda en el sitio de BCN."""
        documents = []

        # Intentar búsqueda en LeyChile
        search_urls = [
            f"{LEYCHILE_URL}/Buscar?tipoBusqueda=rapida&buscar={urlencode({'': query})[1:]}",
            f"{BASE_URL}/buscador/?q={urlencode({'': query})[1:]}&type=norma",
        ]

        for search_url in search_urls:
            html = await self.fetch(search_url)
            if not html:
                continue

            soup = self.parse_html(html, search_url)

            # Buscar resultados
            result_links = soup.find_all("a", href=True)
            count = 0

            for link in result_links:
                if count >= 5:  # máximo 5 resultados por búsqueda
                    break

                href = link.get("href", "")
                title = self.clean_text(link.get_text())

                if not title or len(title) < 10:
                    continue

                # Filtrar solo resultados de leyes/decretos
                href_lower = href.lower()
                title_lower = title.lower()

                if not any(kw in href_lower or kw in title_lower for kw in [
                    "norma", "ley", "decreto", "dfl", "aduana", "arancel", "navegar"
                ]):
                    continue

                full_url = urljoin(search_url, href)

                # Verificar dominio válido
                parsed = urlparse(full_url)
                if "leychile.cl" not in parsed.netloc and "bcn.cl" not in parsed.netloc:
                    continue

                date_text = link.find_parent("li") or link.find_parent("tr") or link
                date = self.extract_date_from_text(date_text.get_text() if hasattr(date_text, "get_text") else "")

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date,
                    "content": f"Resultado de búsqueda: {title}. Consultar texto en: {full_url}",
                    "content_type": self._detect_norma_type(title_lower, href_lower),
                    "source": self.SOURCE_NAME,
                })
                count += 1

            if documents:
                break

        return documents

    def _detect_norma_type(self, title_lower: str, href_lower: str) -> str:
        """Detecta el tipo de norma legal."""
        if "decreto" in title_lower or "dl " in title_lower or "dfl " in title_lower:
            return "decreto"
        if "ley" in title_lower or "ley" in href_lower:
            return "ley"
        if "reglamento" in title_lower:
            return "reglamento"
        if "circular" in title_lower:
            return "circular"
        return "normativa"
