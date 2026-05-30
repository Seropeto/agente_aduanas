"""
Scraper para los Tratados de Libre Comercio (TLC) y Acuerdos Comerciales de Chile.
Fuente: SUBREI — Subsecretaría de Relaciones Económicas Internacionales.
URL base: https://www.subrei.gob.cl

Extrae el texto de los acuerdos comerciales vigentes (TLC Chile-China, Chile-UE,
CPTPP, etc.) para que la normativa de comercio exterior quede indexada como
NORMATIVA OFICIAL recuperable por el motor RAG.
"""
import logging
from typing import Any
from urllib.parse import urljoin, urlparse

from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.subrei.gob.cl"

# Acuerdos comerciales vigentes de interés aduanero.
# Las URLs apuntan a las fichas oficiales de SUBREI por acuerdo.
ACUERDOS_INTERES = [
    {
        "url": f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes/china",
        "title": "Tratado de Libre Comercio Chile - China",
        "content_type": "tratado",
        "date": "2006-10-01",
        "keywords": ["china", "tlc chile china", "tratado libre comercio china"],
    },
    {
        "url": f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes/union-europea",
        "title": "Acuerdo de Asociación Chile - Unión Europea",
        "content_type": "tratado",
        "date": "2003-02-01",
        "keywords": ["union europea", "acuerdo asociacion union europea"],
    },
    {
        "url": f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes/estados-unidos",
        "title": "Tratado de Libre Comercio Chile - Estados Unidos",
        "content_type": "tratado",
        "date": "2004-01-01",
        "keywords": ["estados unidos", "tlc chile estados unidos"],
    },
    {
        "url": f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes/cptpp",
        "title": "CPTPP - Tratado Integral y Progresista de Asociación Transpacífico",
        "content_type": "tratado",
        "date": "2023-02-21",
        "keywords": ["cptpp", "transpacifico", "tpp11"],
    },
    {
        "url": f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes/mercosur",
        "title": "Acuerdo de Complementación Económica Chile - Mercosur",
        "content_type": "tratado",
        "date": "1996-10-01",
        "keywords": ["mercosur", "complementacion economica mercosur"],
    },
]


class TLCScraper(BaseScraper):
    """
    Scraper para los acuerdos comerciales vigentes de Chile publicados por SUBREI.
    Indexa el texto de cada acuerdo como normativa oficial de comercio exterior.
    """

    SOURCE_NAME = "SUBREI - Acuerdos Comerciales de Chile"

    async def scrape(self) -> list[dict[str, Any]]:
        """Ejecuta el scraping de los acuerdos comerciales."""
        documents = []

        async with self as scraper:
            for acuerdo in ACUERDOS_INTERES:
                try:
                    doc = await scraper._scrape_acuerdo(acuerdo)
                    if doc:
                        documents.append(doc)
                        logger.info(f"Obtenido acuerdo: {acuerdo['title']}")
                except Exception as e:
                    logger.error(f"Error scraping acuerdo {acuerdo['title']}: {e}")

        # Deduplicar por URL
        seen_urls = set()
        unique_docs = []
        for doc in documents:
            if doc["url"] not in seen_urls:
                seen_urls.add(doc["url"])
                unique_docs.append(doc)

        logger.info(f"Total acuerdos comerciales únicos: {len(unique_docs)}")
        return unique_docs

    async def _scrape_acuerdo(self, acuerdo_info: dict) -> dict[str, Any] | None:
        """Descarga y extrae el contenido de la ficha de un acuerdo comercial."""
        html = await self.fetch(acuerdo_info["url"])
        if not html:
            # No indexar acuerdos cuya ficha no responde — evita URLs muertas
            logger.warning(f"Acuerdo no disponible, se omite: {acuerdo_info['url']}")
            return None

        soup = self.parse_html(html, acuerdo_info["url"])

        # Eliminar elementos de navegación
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Buscar el cuerpo principal de la ficha del acuerdo
        content_selectors = [
            ("div", {"class": "entry-content"}),
            ("article", {}),
            ("main", {}),
            ("div", {"id": "content"}),
            ("div", {"class": "content"}),
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

        # Reforzar la recuperabilidad: anteponer keywords del acuerdo al contenido
        # para que la búsqueda híbrida (léxica) lo priorice ante consultas directas.
        keywords = acuerdo_info.get("keywords", [])
        keyword_prefix = ""
        if keywords:
            keyword_prefix = f"{acuerdo_info['title']}. Términos relacionados: {', '.join(keywords)}.\n\n"

        # Título desde la página, con fallback al título conocido
        title_el = soup.find("h1") or soup.find("h2") or soup.find("title")
        title = acuerdo_info["title"]
        if title_el:
            extracted_title = self.clean_text(title_el.get_text())
            if extracted_title and len(extracted_title) > 5:
                title = extracted_title

        date = acuerdo_info.get("date", "")
        if not date:
            date = self.extract_date_from_text(content_text[:500])

        return {
            "title": title[:300],
            "url": acuerdo_info["url"],
            "date": date,
            "content": (keyword_prefix + content_text)[:8000],
            "content_type": acuerdo_info.get("content_type", "tratado"),
            "source": self.SOURCE_NAME,
        }

    async def _discover_acuerdos(self) -> list[str]:
        """
        Descubre URLs de acuerdos vigentes desde el índice de SUBREI.
        Reservado para ampliación futura; hoy se usa la lista curada ACUERDOS_INTERES.
        """
        index_url = f"{BASE_URL}/acuerdos-comerciales/acuerdos-comerciales-vigentes"
        html = await self.fetch(index_url)
        if not html:
            return []

        soup = self.parse_html(html, index_url)
        urls = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            full_url = urljoin(index_url, href)
            parsed = urlparse(full_url)
            if "subrei.gob.cl" in parsed.netloc and "acuerdos-comerciales-vigentes/" in full_url:
                if full_url.rstrip("/") != index_url.rstrip("/"):
                    urls.append(full_url)

        return list(dict.fromkeys(urls))  # dedup preservando orden
