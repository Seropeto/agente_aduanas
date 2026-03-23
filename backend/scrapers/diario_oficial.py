"""
Scraper para el Diario Oficial de la República de Chile.
URL base: https://www.diariooficial.interior.gob.cl
Extrae: resoluciones aduaneras, decretos relacionados con importaciones/exportaciones.
"""
import logging
from datetime import date, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse, urlencode

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.diariooficial.interior.gob.cl"

# Términos de búsqueda para filtrar publicaciones aduaneras
SEARCH_TERMS = [
    "aduana",
    "aduanas",
    "arancel",
    "importación",
    "exportación",
    "zona franca",
    "derechos aduaneros",
]

# Secciones del Diario Oficial donde aparecen normas aduaneras
SECTIONS = [
    {
        "url": f"{BASE_URL}/edicion/index.php",
        "label": "Edición actual",
    },
    {
        "url": f"{BASE_URL}/cvs/pages/pub/hacienda/",
        "label": "Publicaciones Hacienda",
    },
]


class DiarioOficialScraper(BaseScraper):
    """
    Scraper para el Diario Oficial de Chile.
    Busca y extrae resoluciones y decretos de carácter aduanero.
    """

    SOURCE_NAME = "Diario Oficial de la República de Chile"

    async def scrape(self) -> list[dict[str, Any]]:
        """Ejecuta el scraping del Diario Oficial."""
        documents = []

        async with self as scraper:
            # Buscar en los últimos 30 días
            docs_recent = await scraper._scrape_recent_editions()
            documents.extend(docs_recent)

            # Búsqueda por términos
            for term in SEARCH_TERMS[:3]:  # limitar para no sobrecargar
                try:
                    search_docs = await scraper._search_term(term)
                    documents.extend(search_docs)
                except Exception as e:
                    logger.error(f"Error buscando '{term}' en Diario Oficial: {e}")

            # Intentar página de búsqueda avanzada
            try:
                adv_docs = await scraper._scrape_advanced_search()
                documents.extend(adv_docs)
            except Exception as e:
                logger.error(f"Error en búsqueda avanzada Diario Oficial: {e}")

        # Deduplicar por URL
        seen_urls = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total documentos únicos del Diario Oficial: {len(unique_docs)}")
        return unique_docs

    async def _scrape_recent_editions(self) -> list[dict[str, Any]]:
        """Obtiene publicaciones recientes del Diario Oficial con contenido aduanero."""
        documents = []

        # Intentar página principal
        html = await self.fetch(BASE_URL)
        if not html:
            # Intentar URL alternativa
            html = await self.fetch(f"{BASE_URL}/edicion/index.php")

        if not html:
            logger.warning("No se pudo acceder al Diario Oficial")
            return documents

        soup = self.parse_html(html, BASE_URL)

        # Buscar fechas de ediciones recientes
        edition_links = soup.find_all("a", href=True)

        for link in edition_links[:50]:
            href = link.get("href", "")
            title = self.clean_text(link.get_text())

            if not title:
                continue

            # Verificar si contiene palabras clave de aduanas
            title_lower = title.lower()
            if not any(term in title_lower for term in SEARCH_TERMS):
                continue

            full_url = urljoin(BASE_URL, href)
            parsed = urlparse(full_url)
            if "diariooficial" not in parsed.netloc and "interior.gob.cl" not in parsed.netloc:
                continue

            date_val = self.extract_date_from_text(title)

            content = await self._fetch_publication_content(full_url, title)

            documents.append({
                "title": title[:300],
                "url": full_url,
                "date": date_val,
                "content": content,
                "content_type": self._detect_doc_type(title_lower),
                "source": self.SOURCE_NAME,
            })

        return documents[:20]

    async def _search_term(self, term: str) -> list[dict[str, Any]]:
        """Busca un término específico en el Diario Oficial."""
        documents = []

        # URL de búsqueda del Diario Oficial
        search_url = f"{BASE_URL}/cvs/pages/pub/index.php?busqueda={term.replace(' ', '+')}"
        search_url_alt = f"{BASE_URL}/busqueda/?q={term.replace(' ', '+')}"

        for url in [search_url, search_url_alt]:
            html = await self.fetch(url)
            if not html:
                continue

            soup = self.parse_html(html, url)

            # Buscar resultados en la página
            result_items = (
                soup.find_all("li", class_=lambda c: c and "result" in c.lower() if c else False)
                or soup.find_all("div", class_=lambda c: c and "result" in c.lower() if c else False)
                or soup.find_all("tr")
            )

            for item in result_items[:10]:
                link_tag = item.find("a", href=True) if hasattr(item, "find") else None
                if not link_tag:
                    continue

                href = link_tag.get("href", "")
                title = self.clean_text(link_tag.get_text())

                if not title or len(title) < 5:
                    # Usar texto del item
                    title = self.clean_text(item.get_text())[:200]

                if not title:
                    continue

                full_url = urljoin(url, href)
                date_val = self.extract_date_from_text(item.get_text())
                content = await self._fetch_publication_content(full_url, title)

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date_val,
                    "content": content,
                    "content_type": self._detect_doc_type(title.lower()),
                    "source": self.SOURCE_NAME,
                })

            if documents:
                break

        return documents

    async def _scrape_advanced_search(self) -> list[dict[str, Any]]:
        """Intenta usar la búsqueda avanzada del Diario Oficial."""
        documents = []

        # URL de búsqueda avanzada con filtro por institución (Hacienda/Aduanas)
        search_urls = [
            f"{BASE_URL}/cvs/pages/pub/index.php?inst=Aduanas",
            f"{BASE_URL}/cvs/pages/pub/hacienda/",
            f"{BASE_URL}/edicion/",
        ]

        for url in search_urls:
            html = await self.fetch(url)
            if not html:
                continue

            soup = self.parse_html(html, url)
            links = soup.find_all("a", href=True)

            for link in links[:30]:
                href = link.get("href", "")
                title = self.clean_text(link.get_text())

                if not title or len(title) < 10:
                    continue

                title_lower = title.lower()
                if not any(term in title_lower for term in SEARCH_TERMS):
                    # También aceptar resoluciones y decretos sin especificar
                    if not any(t in title_lower for t in ["resolución", "resolucion", "decreto", "circular"]):
                        continue

                full_url = urljoin(url, href)
                parsed = urlparse(full_url)
                if "diariooficial" not in parsed.netloc and "interior.gob.cl" not in parsed.netloc:
                    continue

                date_val = self.extract_date_from_text(title)

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date_val,
                    "content": f"Publicación del Diario Oficial: {title}",
                    "content_type": self._detect_doc_type(title_lower),
                    "source": self.SOURCE_NAME,
                })

            if documents:
                break

        return documents[:15]

    async def _fetch_publication_content(self, url: str, fallback: str) -> str:
        """Obtiene el contenido de una publicación del Diario Oficial."""
        if url.lower().endswith(".pdf"):
            return f"Publicación PDF del Diario Oficial: {fallback}"

        html = await self.fetch(url)
        if not html:
            return fallback

        soup = self.parse_html(html, url)
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Buscar el cuerpo de la publicación
        content_el = (
            soup.find("div", id="contenido")
            or soup.find("div", class_="publicacion")
            or soup.find("div", class_="norma")
            or soup.find("article")
            or soup.find("main")
            or soup.body
        )

        if content_el:
            text = self.clean_text(content_el.get_text(separator=" "))
            return text[:6000]

        return self.clean_text(soup.get_text(separator=" "))[:6000]

    def _detect_doc_type(self, title_lower: str) -> str:
        """Detecta el tipo de documento publicado en el Diario Oficial."""
        if "circular" in title_lower:
            return "circular"
        if "resolución" in title_lower or "resolucion" in title_lower:
            return "resolucion"
        if "decreto" in title_lower:
            return "decreto"
        if "ley" in title_lower:
            return "ley"
        if "arancel" in title_lower:
            return "arancel"
        if "instrucción" in title_lower or "instruccion" in title_lower:
            return "instruccion"
        return "publicacion"
