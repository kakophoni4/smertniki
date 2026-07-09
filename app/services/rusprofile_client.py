import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

from app.config import settings
from app.parser.rusprofile import CompanySnapshot, parse_company_html
from app.services.egrul import EgrulClient, InnResolveResult, normalize_inn, parse_egrul_rows

logger = logging.getLogger(__name__)

OGRN_IN_URL_RE = re.compile(r"/id/(\d{13,15})\b")
AJAX_SEARCH = "https://www.rusprofile.ru/ajax.php"


def rusprofile_url(ogrn: str) -> str:
    return f"https://www.rusprofile.ru/id/{ogrn}"


def extract_ogrn_from_search_html(html: str, final_url: str = "") -> str | None:
    m = OGRN_IN_URL_RE.search(final_url or "")
    if m:
        return m.group(1)
    ids = OGRN_IN_URL_RE.findall(html)
    return ids[0] if ids else None


def parse_ajax_search(payload: dict, inn: str) -> InnResolveResult:
    ul = payload.get("ul") or []
    if not ul:
        return InnResolveResult(inn=inn, ogrn=None, error="Компания не найдена в Rusprofile")

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
    """Карточки компаний + опциональный ajax. Резолв ИНН — через EgrulClient."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self.egrul = EgrulClient()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=min(settings.http_timeout_sec, 20), connect=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.rusprofile.ru/",
            },
        )
        await self.egrul.start()

    async def close(self) -> None:
        await self.egrul.close()
        if self._session:
            await self._session.close()
            self._session = None

    async def _throttled_get(self, url: str) -> tuple[str, str]:
        if not self._session:
            raise RuntimeError("RusprofileClient not started")
        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            logger.info("HTTP GET %s", url)
            async with self._session.get(url, allow_redirects=True) as resp:
                text = await resp.text()
                final = str(resp.url)
                logger.info("HTTP %s %s -> %s (%s bytes)", resp.status, url, final, len(text))
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return final, text

    async def fetch(self, ogrn: str) -> str:
        _, html = await self._throttled_get(rusprofile_url(ogrn))
        return html

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        html = await self.fetch(ogrn)
        return parse_company_html(html, ogrn)

    async def resolve_inn(self, inn: str) -> InnResolveResult:
        """ИНН → ОГРН: сначала ЕГРЮЛ ФНС (стабильно с VPS), fallback — Rusprofile ajax."""
        inn_n = normalize_inn(inn) or ""
        if not inn_n:
            return InnResolveResult(inn=inn_n, ogrn=None, error="Некорректный ИНН")

        result = await self.egrul.resolve_inn(inn_n)
        if result.ogrn:
            return result

        # fallback ajax (может 403 с сервера)
        url = f"{AJAX_SEARCH}?query={inn_n}&action=search"
        try:
            if not self._session:
                return result
            async with self._lock:
                await asyncio.sleep(settings.request_delay_sec)
                logger.info("HTTP GET fallback %s", url)
                async with self._session.get(
                    url,
                    headers={
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                ) as resp:
                    body = await resp.text()
                    logger.info("HTTP %s fallback (%s bytes)", resp.status, len(body))
                    if resp.status >= 400:
                        return InnResolveResult(
                            inn=inn_n,
                            ogrn=None,
                            error=f"ЕГРЮЛ: {result.error}; ajax HTTP {resp.status}",
                        )
                    payload = json.loads(body)
            ajax = parse_ajax_search(payload, inn_n)
            if ajax.ogrn:
                logger.info("resolve_inn ajax fallback %s -> %s", inn_n, ajax.ogrn)
                return ajax
            return InnResolveResult(
                inn=inn_n,
                ogrn=None,
                error=f"ЕГРЮЛ: {result.error}; ajax: {ajax.error}",
            )
        except Exception as exc:
            return InnResolveResult(
                inn=inn_n,
                ogrn=None,
                error=f"ЕГРЮЛ: {result.error}; ajax: {exc}",
            )


# re-export for handlers/tests
__all__ = [
    "InnResolveResult",
    "RusprofileClient",
    "extract_ogrn_from_search_html",
    "normalize_inn",
    "parse_ajax_search",
    "parse_egrul_rows",
    "rusprofile_url",
]
