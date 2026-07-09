"""Клиент ЕГРЮЛ ФНС: поиск + выписка PDF + разбор недостоверок.

Поток:
1) POST /  query=ИНН|ОГРН  → token
2) GET /search-result/{token} → rows (имя, ИНН, ОГРН, token выписки)
3) GET /vyp-request/{t} → заказ выписки
4) poll /vyp-status/{t} until ready
5) GET /vyp-download/{t} → PDF
6) parse PDF text
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field

import aiohttp
from pypdf import PdfReader

from app.config import settings

logger = logging.getLogger(__name__)

EGRUL_BASE = "https://egrul.nalog.ru"
INN_RE = re.compile(r"^\d{10}(\d{2})?$")
QUERY_RE = re.compile(r"^\d{10,15}$")
OGRN_RE = re.compile(r"^\d{13,15}$")


@dataclass
class InnResolveResult:
    inn: str
    ogrn: str | None
    name: str | None = None
    final_url: str | None = None
    error: str | None = None
    row_token: str | None = None  # token для скачивания выписки


@dataclass
class CompanySnapshot:
    ogrn: str
    inn: str | None = None
    name: str | None = None
    short_name: str | None = None
    address: str | None = None
    status_text: str | None = None
    unreliable_address: bool = False
    unreliable_director: bool = False
    unreliable_founder: bool = False
    is_liquidating: bool = False
    is_liquidated: bool = False
    signals: list[str] = field(default_factory=list)
    raw_summary: str | None = None

    def has_any_issue(self) -> bool:
        return any(
            [
                self.unreliable_address,
                self.unreliable_director,
                self.unreliable_founder,
                self.is_liquidating,
                self.is_liquidated,
            ]
        )

    def to_dict(self) -> dict:
        return {
            "ogrn": self.ogrn,
            "inn": self.inn,
            "name": self.name,
            "short_name": self.short_name,
            "address": self.address,
            "status_text": self.status_text,
            "unreliable_address": self.unreliable_address,
            "unreliable_director": self.unreliable_director,
            "unreliable_founder": self.unreliable_founder,
            "is_liquidating": self.is_liquidating,
            "is_liquidated": self.is_liquidated,
            "signals": self.signals,
        }


def normalize_inn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if INN_RE.match(digits) else None


def normalize_query(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if QUERY_RE.match(digits) else None


def normalize_ogrn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if OGRN_RE.match(digits) else None


def company_card_url(ogrn: str) -> str:
    """Ссылка «посмотреть глазами» — оставляем публичный агрегатор как convenience."""
    return f"https://www.rusprofile.ru/id/{ogrn}"


def parse_egrul_rows(payload: dict, query: str) -> InnResolveResult:
    rows = payload.get("rows") or []
    if not rows:
        return InnResolveResult(inn=query, ogrn=None, error="Не найдено в ЕГРЮЛ ФНС")

    chosen = None
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
    name = chosen.get("c") or chosen.get("n")
    return InnResolveResult(
        inn=inn or query,
        ogrn=ogrn,
        name=name,
        final_url=company_card_url(ogrn),
        row_token=str(chosen.get("t") or "") or None,
    )


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def parse_vypiska_text(text: str, ogrn: str) -> CompanySnapshot:
    """Разбор текста выписки ЕГРЮЛ (из PDF)."""
    snap = CompanySnapshot(ogrn=ogrn)
    flat = _norm_space(text)
    low = flat.lower()

    # имя
    m = re.search(
        r"сведения о юридическом лице\s+(ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ\s+\"[^\"]+\"|[А-ЯA-Z0-9\"«»\-\s]{5,120}?)\s+ОГРН",
        flat,
        flags=re.I,
    )
    if m:
        snap.name = _norm_space(m.group(1))
        # короткое: ООО "X" из полного
        short = re.search(r"\"([^\"]+)\"", snap.name)
        if short:
            form = "ООО"
            if "АКЦИОНЕР" in snap.name.upper():
                form = "АО"
            snap.short_name = f'{form} "{short.group(1)}"'
        else:
            snap.short_name = snap.name
    if not snap.short_name:
        m = re.search(r'ООО\s*"([^"]+)"', flat)
        if m:
            snap.short_name = f'ООО "{m.group(1)}"'
            snap.name = snap.short_name

    # ИНН
    m = re.search(r"ИНН юридического лица\s+(\d{10})", flat)
    if m:
        snap.inn = m.group(1)
    else:
        m = re.search(r"\bИНН\b\s+(\d{10})", flat)
        if m:
            snap.inn = m.group(1)

    # адрес
    m = re.search(r"Адрес юридического лица\s+(.+?)(?:\d+\s+)?(?:ГРН и дата|Дополнительные сведения|Сведения о)", flat)
    if m:
        snap.address = _norm_space(m.group(1))[:500]

    # --- недостоверки: позиционно по секциям ---
    # После адреса часто идёт «Дополнительные сведения сведения недостоверны»
    addr_block = re.search(
        r"Адрес юридического лица(.+?)(?:Сведения о лице, имеющем право|Сведения о состоянии|Сведения об учредителях)",
        flat,
        flags=re.I | re.S,
    )
    if addr_block and re.search(r"сведения недостоверны", addr_block.group(1), flags=re.I):
        snap.unreliable_address = True
        snap.signals.append("выписка: недостоверность адреса")

    # Блок директора / лица без доверенности
    dir_block = re.search(
        r"Сведения о лице, имеющем право без доверенности(.+?)(?:Сведения об учредителях|Сведения о количестве|Сведения о держателе|Сведения о состоянии|Сведения о видах)",
        flat,
        flags=re.I | re.S,
    )
    if dir_block and re.search(r"сведения недостоверны", dir_block.group(1), flags=re.I):
        snap.unreliable_director = True
        snap.signals.append("выписка: недостоверность ДЛ")

    # Учредители
    found_block = re.search(
        r"Сведения об учредителях(.+?)(?:Сведения о количестве|Сведения о держателе|Сведения о состоянии|Сведения о видах|Сведения о записях)",
        flat,
        flags=re.I | re.S,
    )
    if found_block and re.search(r"сведения недостоверны", found_block.group(1), flags=re.I):
        snap.unreliable_founder = True
        snap.signals.append("выписка: недостоверность учредителя")

    # fallback: если есть «сведения недостоверны» но секции не поймали — хотя бы адрес
    if (
        not snap.unreliable_address
        and not snap.unreliable_director
        and not snap.unreliable_founder
        and re.search(r"сведения недостоверны", low)
    ):
        # эвристика: первая недостоверность обычно адрес
        snap.unreliable_address = True
        snap.signals.append("выписка: недостоверность (без точной секции→адрес)")

    # ликвидация / исключение
    if re.search(r"предстоящем исключении юридического лица из егрюл", low):
        snap.is_liquidating = True
        snap.status_text = "Предстоящее исключение из ЕГРЮЛ"
        snap.signals.append("выписка: предстоящее исключение")
    elif re.search(r"в процессе ликвидации", low):
        snap.is_liquidating = True
        snap.status_text = "В процессе ликвидации"
        snap.signals.append("выписка: ликвидация")
    elif re.search(r"исключен[оа]?\s+из\s+егрюл|ликвидирован", low):
        snap.is_liquidated = True
        snap.status_text = "Исключено / ликвидировано"
        snap.signals.append("выписка: исключено/ликвидировано")
    elif not snap.status_text:
        snap.status_text = "Действующая" if not snap.has_any_issue() else "Есть отметки в ЕГРЮЛ"

    snap.raw_summary = "; ".join(snap.signals) if snap.signals else "ok"
    return snap


class EgrulClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=min(settings.http_timeout_sec, 60), connect=15)
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
        return await self.search(inn)

    async def search(self, query: str) -> InnResolveResult:
        q = normalize_query(query) or ""
        if not q:
            return InnResolveResult(inn=query, ogrn=None, error="Некорректный ИНН/ОГРН")
        if not self._session:
            raise RuntimeError("EgrulClient not started")

        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            try:
                data = {
                    "query": q,
                    "vyp3CaptchaToken": "",
                    "page": "",
                    "PreventChromeAutocomplete": "",
                }
                logger.info("EGRUL POST search %s", q)
                async with self._session.post(
                    f"{EGRUL_BASE}/",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                ) as resp:
                    body = await resp.text()
                    logger.info("EGRUL POST %s (%s bytes)", resp.status, len(body))
                    if resp.status >= 400:
                        return InnResolveResult(inn=q, ogrn=None, error=f"EGRUL HTTP {resp.status}")
                    payload = await resp.json(content_type=None)

                token = payload.get("t")
                if not token:
                    if payload.get("captchaRequired"):
                        return InnResolveResult(inn=q, ogrn=None, error="ЕГРЮЛ требует капчу")
                    return InnResolveResult(inn=q, ogrn=None, error="ЕГРЮЛ не вернул token")

                result_payload = None
                for attempt in range(5):
                    await asyncio.sleep(0.6 if attempt else 0.3)
                    url = f"{EGRUL_BASE}/search-result/{token}"
                    async with self._session.get(url) as resp:
                        text = await resp.text()
                        logger.info("EGRUL GET result attempt=%s HTTP %s", attempt + 1, resp.status)
                        if resp.status >= 400:
                            continue
                        result_payload = await resp.json(content_type=None)
                        if result_payload.get("rows"):
                            break

                if not result_payload:
                    return InnResolveResult(inn=q, ogrn=None, error="ЕГРЮЛ: пустой результат")

                result = parse_egrul_rows(result_payload, q)
                if result.ogrn:
                    logger.info("EGRUL search %s -> %s (%s)", q, result.ogrn, result.name)
                return result
            except Exception as exc:
                logger.warning("EGRUL search failed for %s: %s", q, exc)
                return InnResolveResult(inn=q, ogrn=None, error=str(exc))

    async def download_vypiska_pdf(self, row_token: str) -> bytes:
        if not self._session:
            raise RuntimeError("EgrulClient not started")

        async with self._lock:
            await asyncio.sleep(settings.request_delay_sec)
            logger.info("EGRUL vyp-request")
            async with self._session.get(f"{EGRUL_BASE}/vyp-request/{row_token}") as resp:
                body = await resp.text()
                logger.info("EGRUL vyp-request HTTP %s (%s bytes)", resp.status, len(body))
                if resp.status >= 400:
                    raise RuntimeError(f"vyp-request HTTP {resp.status}")
                payload = await resp.json(content_type=None)
                if payload.get("captchaRequired"):
                    raise RuntimeError("ЕГРЮЛ требует капчу на выписке")

            for attempt in range(20):
                await asyncio.sleep(1.5 if attempt else 0.5)
                async with self._session.get(f"{EGRUL_BASE}/vyp-status/{row_token}") as resp:
                    st = await resp.text()
                    logger.info("EGRUL vyp-status[%s]=%s", attempt + 1, st[:80])
                    if "ready" in st.lower():
                        break
            else:
                raise RuntimeError("Выписка ЕГРЮЛ не готова (timeout)")

            async with self._session.get(f"{EGRUL_BASE}/vyp-download/{row_token}") as resp:
                data = await resp.read()
                ctype = resp.headers.get("Content-Type", "")
                logger.info("EGRUL vyp-download HTTP %s type=%s size=%s", resp.status, ctype, len(data))
                if resp.status >= 400:
                    raise RuntimeError(f"vyp-download HTTP {resp.status}")
                if not data.startswith(b"%PDF"):
                    raise RuntimeError(f"Ожидали PDF, получили {ctype} ({len(data)} bytes)")
                return data

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        """Полная проверка компании через выписку ЕГРЮЛ."""
        ogrn_n = normalize_ogrn(ogrn) or ogrn
        search = await self.search(ogrn_n)
        if not search.ogrn:
            raise RuntimeError(search.error or "Не найдено в ЕГРЮЛ")
        if search.ogrn != ogrn_n:
            raise RuntimeError(f"ЕГРЮЛ вернул другой ОГРН: {search.ogrn} вместо {ogrn_n}")
        if not search.row_token:
            raise RuntimeError("Нет token выписки в ответе ЕГРЮЛ")

        pdf = await self.download_vypiska_pdf(search.row_token)
        text = extract_text_from_pdf(pdf)
        snap = parse_vypiska_text(text, ogrn_n)
        # подстрахуем имя/инн из search, если PDF криво распарсился
        if not snap.short_name and search.name:
            snap.short_name = search.name
            snap.name = search.name
        if not snap.inn and search.inn and len(str(search.inn)) in (10, 12):
            snap.inn = str(search.inn)
        logger.info(
            "EGRUL snapshot %s name=%s addr=%s dir=%s found=%s liq=%s signals=%s",
            ogrn_n,
            snap.short_name,
            snap.unreliable_address,
            snap.unreliable_director,
            snap.unreliable_founder,
            snap.is_liquidating or snap.is_liquidated,
            snap.raw_summary,
        )
        return snap
