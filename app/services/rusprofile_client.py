import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from app.config import settings
from app.parser.rusprofile import CompanySnapshot, parse_company_html

logger = logging.getLogger(__name__)

OGRN_IN_URL_RE = re.compile(r"/id/(\d{13,15})\b")
INN_RE = re.compile(r"^\d{10}(\d{2})?$")
AJAX_SEARCH = "https://www.rusprofile.ru/ajax.php"


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
    """Fallback для HTML (основной путь — ajax JSON)."""
    m = OGRN_IN_URL_RE.search(final_url or "")
    if m:
        return m.group(1)
    ids = OGRN_IN_URL_RE.findall(html)
    return ids[0] if ids else None


def parse_ajax_search(payload: dict, inn: str) -> InnResolveResult:
    """Парсит ответ ajax.php?action=search."""
    ul = payload.get("ul") or []
    if not ul:
        return InnResolveResult(inn=inn, ogrn=None, error="Компания не найдена в Rusprofile")

    # точное совпадение ИНН, иначе первая юрлицо
    chosen = None
    for item in ul:
        raw_inn = re.sub(r"\D", "", str(item.get("inn") or ""))
        if raw_inn == inn:
            chosen = item
            break
    if chosen is None:
        chosen = ul[0]

    ogrn = str(chosen.get("ogrn") or chosen.get("raw_ogrn") or "").strip()
    if not ogrn or not re.fullmatch(r"\d{13,15}", ogrn):
        link = str(chosen.get("link") or chosen.get("url") or "")
        m = OGRN_IN_URL_RE.search(link)
        ogrn = m.group(1) if m else ""

    if not ogrn:
        return InnResolveResult(inn=inn, ogrn=None, error="В ответе ajax нет ОГРН")

    name = chosen.get("name") or chosen.get("raw_name")
    return InnResolveResult(
        inn=inn,
        ogrn=ogrn,
        name=name,
        final_url=rusprofile_url(ogrn),
    )


class RusprofileClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=min(settings.http_timeout_sec, 20), connect=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.rusprofile.ru/",
            },
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _throttled_get(self, url: str, accept_html: bool = False) -> tuple[str, str]:
        if not self._session:
            raise RuntimeError("RusprofileClient not started")
        headers = {}
        if accept_html:
            headers["Accept"] = "text/html,application/xhtml+xml"
        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            logger.info("HTTP GET %s", url)
            async with self._session.get(url, allow_redirects=True, headers=headers or None) as resp:
                text = await resp.text()
                final = str(resp.url)
                logger.info("HTTP %s %s -> %s (%s bytes)", resp.status, url, final, len(text))
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return final, text

    async def fetch(self, ogrn: str) -> str:
        _, html = await self._throttled_get(rusprofile_url(ogrn), accept_html=True)
        return html

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        html = await self.fetch(ogrn)
        return parse_company_html(html, ogrn)

    async def resolve_inn(self, inn: str) -> InnResolveResult:
        """ИНН → ОГРН через рабочий ajax API Rusprofile (не /search — он 404)."""
        inn = normalize_inn(inn) or ""
        if not inn:
            return InnResolveResult(inn=inn, ogrn=None, error="Некорректный ИНН")

        url = f"{AJAX_SEARCH}?query={inn}&action=search"
        try:
            _, body = await self._throttled_get(url)
            payload = json.loads(body)
        except Exception as exc:
            logger.warning("resolve_inn ajax failed for %s: %s", inn, exc)
            return InnResolveResult(inn=inn, ogrn=None, error=str(exc))

        result = parse_ajax_search(payload, inn)
        if result.ogrn:
            logger.info("resolve_inn %s -> %s (%s)", inn, result.ogrn, result.name)
        else:
            logger.warning("resolve_inn miss %s: %s", inn, result.error)
        return result
