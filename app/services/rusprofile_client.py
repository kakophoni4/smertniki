import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.parser.rusprofile import CompanySnapshot, parse_company_html

logger = logging.getLogger(__name__)

OGRN_IN_URL_RE = re.compile(r"/id/(\d{13,15})\b")
OGRN_IN_TEXT_RE = re.compile(r"ОГРН\s*[:\s]?\s*(\d{13,15})", re.IGNORECASE)
INN_RE = re.compile(r"^\d{10}(\d{2})?$")


def rusprofile_url(ogrn: str) -> str:
    return f"https://www.rusprofile.ru/id/{ogrn}"


def normalize_inn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if INN_RE.match(digits) else None


@dataclass
class InnResolveResult:
    inn: str
    ogrn: str | None
    name: str | None = None
    final_url: str | None = None
    error: str | None = None


def extract_ogrn_from_search_html(html: str, final_url: str = "") -> str | None:
    """Достаёт ОГРН из редиректа на карточку или из выдачи поиска."""
    m = OGRN_IN_URL_RE.search(final_url or "")
    if m:
        return m.group(1)

    # прямые ссылки на карточки в выдаче
    ids = OGRN_IN_URL_RE.findall(html)
    if ids:
        # самый частый / первый — обычно нужная компания
        return ids[0]

    m = OGRN_IN_TEXT_RE.search(html)
    if m:
        return m.group(1)
    return None


def extract_name_near_ogrn(html: str, ogrn: str) -> str | None:
    # грубо: ищем h1 или title рядом
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        name = re.sub(r"<[^>]+>", "", m.group(1))
        name = " ".join(name.split()).strip()
        if name:
            return name
    return None


class RusprofileClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=settings.http_timeout_sec)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _throttled_get(self, url: str) -> tuple[str, str]:
        if not self._session:
            raise RuntimeError("RusprofileClient not started")
        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            async with self._session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                return str(resp.url), await resp.text()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    async def fetch(self, ogrn: str) -> str:
        _, html = await self._throttled_get(rusprofile_url(ogrn))
        return html

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        html = await self.fetch(ogrn)
        return parse_company_html(html, ogrn)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    async def resolve_inn(self, inn: str) -> InnResolveResult:
        """Отдельный шаг: ИНН → ОГРН через поиск Rusprofile. В мониторинг не добавляет."""
        inn = normalize_inn(inn) or ""
        if not inn:
            return InnResolveResult(inn=inn, ogrn=None, error="Некорректный ИНН")

        # основной поиск; если редирект на /id/ — сразу ОГРН
        url = f"https://www.rusprofile.ru/search?query={inn}"
        try:
            final_url, html = await self._throttled_get(url)
        except Exception as exc:
            return InnResolveResult(inn=inn, ogrn=None, error=str(exc))

        ogrn = extract_ogrn_from_search_html(html, final_url)
        if not ogrn:
            return InnResolveResult(
                inn=inn,
                ogrn=None,
                final_url=final_url,
                error="ОГРН не найден в выдаче Rusprofile",
            )

        name = extract_name_near_ogrn(html, ogrn)
        return InnResolveResult(inn=inn, ogrn=ogrn, name=name, final_url=final_url)
