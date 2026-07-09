"""Фасад мониторинга: источник правды — ЕГРЮЛ ФНС.

Rusprofile больше не парсим (антибот/403). Ссылка на карточку
остаётся только как convenience в уведомлениях.
"""

from app.services.egrul import (
    CompanySnapshot,
    EgrulClient,
    InnResolveResult,
    company_card_url,
    normalize_inn,
    normalize_ogrn,
    normalize_query,
    parse_egrul_rows,
    parse_vypiska_text,
)

# backward-compatible names used across the codebase
rusprofile_url = company_card_url


class RusprofileClient:
    """Историческое имя. Внутри — только ЕГРЮЛ."""

    def __init__(self) -> None:
        self.egrul = EgrulClient()

    async def start(self) -> None:
        await self.egrul.start()

    async def close(self) -> None:
        await self.egrul.close()

    async def resolve_inn(self, inn: str) -> InnResolveResult:
        return await self.egrul.search(inn)

    async def get_snapshot(self, ogrn: str) -> CompanySnapshot:
        return await self.egrul.get_snapshot(ogrn)


__all__ = [
    "CompanySnapshot",
    "InnResolveResult",
    "RusprofileClient",
    "company_card_url",
    "normalize_inn",
    "normalize_ogrn",
    "normalize_query",
    "parse_egrul_rows",
    "parse_vypiska_text",
    "rusprofile_url",
]
