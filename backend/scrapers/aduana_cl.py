"""
Scraper para el sitio oficial del Servicio Nacional de Aduanas de Chile.
URL base: https://www.aduana.cl
Extrae: circulares, resoluciones, arancel aduanero, procedimientos.
"""
import logging
from typing import Any
from urllib.parse import urljoin, urlparse

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.aduana.cl"

# Secciones de interés en el sitio de Aduanas
SECTIONS = [
    {
        "url": f"{BASE_URL}/aduana/612/w3-propertyname-570.html",
        "content_type": "circular",
        "label": "Circulares",
    },
    {
        "url": f"{BASE_URL}/aduana/612/w3-propertyname-571.html",
        "content_type": "resolucion",
        "label": "Resoluciones",
    },
    {
        "url": f"{BASE_URL}/aduana/612/w3-propertyname-572.html",
        "content_type": "arancel",
        "label": "Arancel Aduanero",
    },
    {
        "url": f"{BASE_URL}/aduana/612/w3-propertyname-2511.html",
        "content_type": "procedimiento",
        "label": "Procedimientos",
    },
    {
        "url": f"{BASE_URL}/aduana/612/w3-channel.html",
        "content_type": "normativa",
        "label": "Normativa General",
    },
]

# URLs alternativas de búsqueda en caso de que las anteriores no respondan
FALLBACK_URLS = [
    {
        "url": f"{BASE_URL}/aduana/613/w3-channel.html",
        "content_type": "circular",
        "label": "Circulares (fallback)",
    },
    {
        "url": f"{BASE_URL}/aduana/614/w3-channel.html",
        "content_type": "resolucion",
        "label": "Resoluciones (fallback)",
    },
]


class AduanaScraper(BaseScraper):
    """
    Scraper para www.aduana.cl.
    Extrae circulares, resoluciones, aranceles y procedimientos aduaneros.
    """

    SOURCE_NAME = "Servicio Nacional de Aduanas"

    async def scrape(self) -> list[dict[str, Any]]:
        """Ejecuta el scraping completo del sitio de Aduanas."""
        documents = []

        async with self as scraper:
            # Primero intentar página principal para obtener estructura
            main_docs = await scraper._scrape_main_page()
            documents.extend(main_docs)

            # Recorrer secciones específicas
            for section in SECTIONS:
                try:
                    section_docs = await scraper._scrape_section(
                        section["url"],
                        section["content_type"],
                    )
                    documents.extend(section_docs)
                    logger.info(
                        f"Sección '{section['label']}': {len(section_docs)} documentos"
                    )
                except Exception as e:
                    logger.error(f"Error en sección {section['label']}: {e}")

            # Fallbacks si no se obtuvieron suficientes documentos
            if len(documents) < 5:
                for section in FALLBACK_URLS:
                    try:
                        section_docs = await scraper._scrape_section(
                            section["url"],
                            section["content_type"],
                        )
                        documents.extend(section_docs)
                    except Exception as e:
                        logger.error(f"Error en fallback {section['label']}: {e}")

        # Deduplicar por URL
        seen_urls = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total documentos únicos de Aduanas: {len(unique_docs)}")
        return unique_docs

    async def _scrape_main_page(self) -> list[dict[str, Any]]:
        """Scrape la página principal de normativa de Aduanas."""
        documents = []
        urls_to_try = [
            f"{BASE_URL}/aduana/612/w3-channel.html",
            f"{BASE_URL}/normativa/",
            BASE_URL,
        ]

        for url in urls_to_try:
            html = await self.fetch(url)
            if not html:
                continue

            soup = self.parse_html(html, BASE_URL)

            # Buscar enlaces a documentos normativos
            links = soup.find_all("a", href=True)
            for link in links:
                href = link.get("href", "")
                text = self.clean_text(link.get_text())

                if not text or len(text) < 5:
                    continue

                # Filtrar por palabras clave de interés aduanero
                keywords = [
                    "circular", "resolución", "resolución", "arancel",
                    "procedimiento", "norma", "decreto", "instrucción",
                    "clasificación", "valoración", "origen",
                ]
                text_lower = text.lower()
                if not any(kw in text_lower for kw in keywords):
                    continue

                full_url = urljoin(BASE_URL, href)
                if not self._is_valid_aduana_url(full_url):
                    continue

                content_type = self._detect_content_type(text_lower, href)
                date = self.extract_date_from_text(text)

                documents.append({
                    "title": text[:200],
                    "url": full_url,
                    "date": date,
                    "content": text,
                    "content_type": content_type,
                    "source": self.SOURCE_NAME,
                })

            if documents:
                break

        return documents

    async def _scrape_section(
        self, url: str, content_type: str
    ) -> list[dict[str, Any]]:
        """Scrape una sección específica del sitio de Aduanas."""
        documents = []

        html = await self.fetch(url)
        if not html:
            return documents

        soup = self.parse_html(html, BASE_URL)

        # Buscar tablas con listados de documentos
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue

                # Extraer enlace y texto
                link_tag = row.find("a", href=True)
                if not link_tag:
                    continue

                href = link_tag.get("href", "")
                title = self.clean_text(link_tag.get_text())
                if not title:
                    title = self.clean_text(" ".join(c.get_text() for c in cells))

                full_url = urljoin(BASE_URL, href)
                if not self._is_valid_url(full_url):
                    continue

                # Fecha desde las celdas
                date_text = " ".join(c.get_text() for c in cells)
                date = self.extract_date_from_text(date_text)

                # Intentar obtener el contenido del documento
                content = await self._fetch_document_content(full_url, title)

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date,
                    "content": content,
                    "content_type": content_type,
                    "source": self.SOURCE_NAME,
                })

        # Buscar listados en divs/ul/li si no hay tablas
        if not documents:
            list_items = soup.find_all(["li", "div"], class_=lambda c: c and (
                "item" in c.lower() or "doc" in c.lower() or "list" in c.lower()
            ))

            for item in list_items[:30]:  # limitar a 30 items por sección
                link_tag = item.find("a", href=True)
                if not link_tag:
                    continue

                href = link_tag.get("href", "")
                title = self.clean_text(link_tag.get_text())
                if not title or len(title) < 5:
                    continue

                full_url = urljoin(BASE_URL, href)
                if not self._is_valid_url(full_url):
                    continue

                date = self.extract_date_from_text(item.get_text())
                content = await self._fetch_document_content(full_url, title)

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date,
                    "content": content,
                    "content_type": content_type,
                    "source": self.SOURCE_NAME,
                })

        # Si aún no hay documentos, extraer cualquier enlace relevante de la página
        if not documents:
            all_links = soup.find_all("a", href=True)
            for link in all_links[:50]:
                href = link.get("href", "")
                title = self.clean_text(link.get_text())

                if not title or len(title) < 10:
                    continue

                full_url = urljoin(BASE_URL, href)
                if not self._is_valid_aduana_url(full_url):
                    continue

                date = self.extract_date_from_text(title)

                documents.append({
                    "title": title[:300],
                    "url": full_url,
                    "date": date,
                    "content": title,
                    "content_type": content_type,
                    "source": self.SOURCE_NAME,
                })

        return documents[:50]  # máximo 50 documentos por sección

    async def _fetch_document_content(self, url: str, fallback_title: str) -> str:
        """Obtiene el contenido de texto de un documento o página."""
        if url.lower().endswith(".pdf"):
            # No descargar PDFs aquí, solo registrar la URL
            return f"Documento PDF: {fallback_title}"

        html = await self.fetch(url)
        if not html:
            return fallback_title

        soup = self.parse_html(html, BASE_URL)

        # Eliminar scripts y estilos
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # Buscar el contenido principal
        main_content = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", id="content")
            or soup.find("div", class_=lambda c: c and "content" in c.lower())
            or soup.find("div", id="main")
            or soup.body
        )

        if main_content:
            text = self.clean_text(main_content.get_text(separator=" "))
        else:
            text = self.clean_text(soup.get_text(separator=" "))

        # Limitar longitud
        return text[:5000] if text else fallback_title

    def _detect_content_type(self, text_lower: str, href: str) -> str:
        """Detecta el tipo de contenido basado en texto y URL."""
        href_lower = href.lower()
        if "circular" in text_lower or "circ" in href_lower:
            return "circular"
        if "resolución" in text_lower or "resolucion" in href_lower or "res_" in href_lower:
            return "resolucion"
        if "arancel" in text_lower or "arancel" in href_lower:
            return "arancel"
        if "procedimiento" in text_lower or "proc" in href_lower:
            return "procedimiento"
        if "decreto" in text_lower or "dl" in href_lower:
            return "decreto"
        return "normativa"

    def _is_valid_url(self, url: str) -> bool:
        """Verifica si una URL es válida para procesar."""
        try:
            parsed = urlparse(url)
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    def _is_valid_aduana_url(self, url: str) -> bool:
        """Verifica si la URL pertenece al dominio de Aduanas."""
        try:
            parsed = urlparse(url)
            return (
                parsed.scheme in ("http", "https")
                and "aduana.cl" in parsed.netloc
            )
        except Exception:
            return False
