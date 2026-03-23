import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Clase base para todos los scrapers de fuentes aduaneras chilenas."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0  # segundos entre reintentos
    REQUEST_TIMEOUT = 30.0
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }

    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.logger = logging.getLogger(self.__class__.__name__)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers=self.DEFAULT_HEADERS,
            timeout=self.REQUEST_TIMEOUT,
            follow_redirects=True,
            verify=False,  # algunos sitios gov.cl tienen cert issues
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    async def fetch(self, url: str, **kwargs) -> Optional[str]:
        """Descarga una URL con reintentos y manejo de errores."""
        if not self.client:
            raise RuntimeError("Usar dentro de un bloque 'async with'")

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                self.logger.debug(f"Descargando {url} (intento {attempt})")
                response = await self.client.get(url, **kwargs)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as e:
                self.logger.warning(
                    f"Error HTTP {e.response.status_code} en {url} (intento {attempt})"
                )
                if e.response.status_code in (403, 404, 410):
                    # No reintentar errores permanentes
                    return None
            except httpx.TimeoutException:
                self.logger.warning(f"Timeout en {url} (intento {attempt})")
            except httpx.RequestError as e:
                self.logger.warning(f"Error de red en {url}: {e} (intento {attempt})")
            except Exception as e:
                self.logger.error(f"Error inesperado en {url}: {e}")
                return None

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_DELAY * attempt)

        self.logger.error(f"Falló la descarga de {url} después de {self.MAX_RETRIES} intentos")
        return None

    async def fetch_bytes(self, url: str, **kwargs) -> Optional[bytes]:
        """Descarga una URL y retorna bytes (para PDFs)."""
        if not self.client:
            raise RuntimeError("Usar dentro de un bloque 'async with'")

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = await self.client.get(url, **kwargs)
                response.raise_for_status()
                return response.content
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError) as e:
                self.logger.warning(f"Error descargando bytes de {url}: {e} (intento {attempt})")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY * attempt)
            except Exception as e:
                self.logger.error(f"Error inesperado descargando bytes: {e}")
                return None

        return None

    def parse_html(self, html: str, base_url: str = "") -> BeautifulSoup:
        """Parsea HTML con lxml como parser primario."""
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            return BeautifulSoup(html, "html.parser")

    def clean_text(self, text: str) -> str:
        """Limpia y normaliza el texto extraído."""
        if not text:
            return ""
        # Normalizar espacios en blanco
        text = re.sub(r"\s+", " ", text)
        # Eliminar caracteres de control
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        # Eliminar líneas vacías múltiples
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def extract_date_from_text(self, text: str) -> str:
        """Intenta extraer una fecha del texto en varios formatos."""
        if not text:
            return ""
        # dd/mm/yyyy o dd-mm-yyyy
        m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
        # yyyy-mm-dd
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return m.group(0)
        # Mes escrito en español
        meses = {
            "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
            "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
            "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
        }
        pattern = r"(\d{1,2})\s+de\s+(" + "|".join(meses.keys()) + r")\s+(?:de\s+)?(\d{4})"
        m = re.search(pattern, text.lower())
        if m:
            d = m.group(1).zfill(2)
            mo = meses[m.group(2)]
            y = m.group(3)
            return f"{y}-{mo}-{d}"
        return ""

    @abstractmethod
    async def scrape(self) -> list[dict[str, Any]]:
        """
        Método principal de scraping. Retorna lista de documentos con estructura:
        {
            "title": str,
            "url": str,
            "date": str (YYYY-MM-DD),
            "content": str,
            "content_type": str,
            "source": str,
        }
        """
        pass
