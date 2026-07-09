import asyncio
import logging

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.parser.rusprofile import CompanySnapshot, parse_company_html

logger = logging.getLogger(__name__)


def rusprofile_url(ogrn: str) -> str:
    return f"https://www.rusprofile.ru/id/{ogrn}"


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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    async def fetch(self, ogrn: str) -> str:
        if not self._session:
            raise RuntimeError("RusprofileClient not started")
        url = rusprofile_url(ogrn)
        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        html = await self.fetch(ogrn)
        return parse_company_html(html, ogrn)
