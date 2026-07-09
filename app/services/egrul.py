"""Резолв ИНН → ОГРН через egrul.nalog.ru (официальный ЕГРЮЛ).

Rusprofile ajax с VPS часто ловит 403 после нескольких запросов —
для импорта списка ИНН используем ФНС.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

EGRUL_BASE = "https://egrul.nalog.ru"
INN_RE = re.compile(r"^\d{10}(\d{2})?$")
QUERY_RE = re.compile(r"^\d{10,15}$")


@dataclass
class InnResolveResult:
    inn: str
    ogrn: str | None
    name: str | None = None
    final_url: str | None = None
    error: str | None = None


def normalize_inn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if INN_RE.match(digits) else None


def normalize_query(value: str) -> str | None:
    """ИНН (10/12) или ОГРН (13/15) — оба валидны для поиска ЕГРЮЛ."""
    digits = re.sub(r"\D", "", value.strip())
    return digits if QUERY_RE.match(digits) else None


def parse_egrul_rows(payload: dict, query: str) -> InnResolveResult:
    rows = payload.get("rows") or []
    if not rows:
        return InnResolveResult(inn=query, ogrn=None, error="Не найдено в ЕГРЮЛ ФНС")

    chosen = None
    # точное совпадение по ИНН или ОГРН
    for row in rows:
        if str(row.get("i") or "") == query or str(row.get("o") or "") == query:
            if str(row.get("k") or "") == "ul":
                chosen = row
                break
            if chosen is None:
                chosen = row
    if chosen is None:
        chosen = rows[0]

    ogrn = str(chosen.get("o") or "").strip()
    if not re.fullmatch(r"\d{13,15}", ogrn):
        return InnResolveResult(inn=query, ogrn=None, error="В ответе ЕГРЮЛ нет ОГРН")

    inn = str(chosen.get("i") or "").strip() or (query if len(query) in (10, 12) else None)
    name = chosen.get("c") or chosen.get("n")  # короткое имя предпочтительнее
    return InnResolveResult(
        inn=inn or query,
        ogrn=ogrn,
        name=name,
        final_url=f"https://www.rusprofile.ru/id/{ogrn}",
    )


class EgrulClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=min(settings.http_timeout_sec, 25), connect=10)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.CookieJar(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": EGRUL_BASE,
                "Referer": f"{EGRUL_BASE}/",
            },
        )
        # прогрев cookies
        try:
            async with self._session.get(f"{EGRUL_BASE}/") as resp:
                await resp.read()
                logger.info("EGRUL warmup HTTP %s", resp.status)
        except Exception as exc:
            logger.warning("EGRUL warmup failed: %s", exc)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def resolve_inn(self, inn: str) -> InnResolveResult:
        """Поиск по ИНН или ОГРН (ЕГРЮЛ принимает оба)."""
        query = normalize_query(inn) or ""
        if not query:
            return InnResolveResult(inn=inn, ogrn=None, error="Некорректный ИНН/ОГРН")
        if not self._session:
            raise RuntimeError("EgrulClient not started")

        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            try:
                data = {
                    "query": query,
                    "vyp3CaptchaToken": "",
                    "page": "",
                    "PreventChromeAutocomplete": "",
                }
                logger.info("EGRUL POST search %s", query)
                async with self._session.post(
                    f"{EGRUL_BASE}/",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                ) as resp:
                    body = await resp.text()
                    logger.info("EGRUL POST %s (%s bytes)", resp.status, len(body))
                    if resp.status >= 400:
                        return InnResolveResult(inn=query, ogrn=None, error=f"EGRUL HTTP {resp.status}")
                    payload = await resp.json(content_type=None)

                token = payload.get("t")
                if not token:
                    if payload.get("captchaRequired"):
                        return InnResolveResult(inn=query, ogrn=None, error="ЕГРЮЛ требует капчу")
                    return InnResolveResult(inn=query, ogrn=None, error="ЕГРЮЛ не вернул token")

                result_payload = None
                for attempt in range(4):
                    await asyncio.sleep(0.7 if attempt else 0.3)
                    url = f"{EGRUL_BASE}/search-result/{token}"
                    logger.info("EGRUL GET result attempt=%s", attempt + 1)
                    async with self._session.get(url) as resp:
                        text = await resp.text()
                        logger.info("EGRUL GET %s (%s bytes)", resp.status, len(text))
                        if resp.status >= 400:
                            continue
                        result_payload = await resp.json(content_type=None)
                        if result_payload.get("rows"):
                            break

                if not result_payload:
                    return InnResolveResult(inn=query, ogrn=None, error="ЕГРЮЛ: пустой результат")

                result = parse_egrul_rows(result_payload, query)
                if result.ogrn:
                    logger.info("EGRUL resolve %s -> %s (%s)", query, result.ogrn, result.name)
                else:
                    logger.warning("EGRUL miss %s: %s", query, result.error)
                return result
            except Exception as exc:
                logger.warning("EGRUL resolve failed for %s: %s", query, exc)
                return InnResolveResult(inn=query, ogrn=None, error=str(exc))
