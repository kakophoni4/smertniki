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


def parse_egrul_rows(payload: dict, inn: str) -> InnResolveResult:
    rows = payload.get("rows") or []
    if not rows:
        return InnResolveResult(inn=inn, ogrn=None, error="Не найдено в ЕГРЮЛ ФНС")

    chosen = None
    for row in rows:
        if str(row.get("i") or "") == inn and str(row.get("k") or "") == "ul":
            chosen = row
            break
    if chosen is None:
        for row in rows:
            if str(row.get("i") or "") == inn:
                chosen = row
                break
    if chosen is None:
        chosen = rows[0]

    ogrn = str(chosen.get("o") or "").strip()
    if not re.fullmatch(r"\d{13,15}", ogrn):
        return InnResolveResult(inn=inn, ogrn=None, error="В ответе ЕГРЮЛ нет ОГРН")

    name = chosen.get("n") or chosen.get("c")
    return InnResolveResult(
        inn=inn,
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
        inn = normalize_inn(inn) or ""
        if not inn:
            return InnResolveResult(inn=inn, ogrn=None, error="Некорректный ИНН")
        if not self._session:
            raise RuntimeError("EgrulClient not started")

        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            try:
                data = {
                    "query": inn,
                    "vyp3CaptchaToken": "",
                    "page": "",
                    "PreventChromeAutocomplete": "",
                }
                logger.info("EGRUL POST search %s", inn)
                async with self._session.post(
                    f"{EGRUL_BASE}/",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                ) as resp:
                    body = await resp.text()
                    logger.info("EGRUL POST %s (%s bytes)", resp.status, len(body))
                    if resp.status >= 400:
                        return InnResolveResult(inn=inn, ogrn=None, error=f"EGRUL HTTP {resp.status}")
                    payload = await resp.json(content_type=None)

                token = payload.get("t")
                if not token:
                    if payload.get("captchaRequired"):
                        return InnResolveResult(inn=inn, ogrn=None, error="ЕГРЮЛ требует капчу")
                    return InnResolveResult(inn=inn, ogrn=None, error="ЕГРЮЛ не вернул token")

                # результат иногда появляется с небольшой задержкой
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
                    return InnResolveResult(inn=inn, ogrn=None, error="ЕГРЮЛ: пустой результат")

                result = parse_egrul_rows(result_payload, inn)
                if result.ogrn:
                    logger.info("EGRUL resolve %s -> %s (%s)", inn, result.ogrn, result.name)
                else:
                    logger.warning("EGRUL miss %s: %s", inn, result.error)
                return result
            except Exception as exc:
                logger.warning("EGRUL resolve failed for %s: %s", inn, exc)
                return InnResolveResult(inn=inn, ogrn=None, error=str(exc))
