from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company
from app.services.rusprofile_client import rusprofile_url


async def upsert_company(
    session: AsyncSession,
    ogrn: str,
    inn: str | None = None,
    name: str | None = None,
) -> tuple[Company, bool]:
    existing = None
    if inn:
        existing = await session.scalar(select(Company).where(Company.inn == inn))
    if not existing:
        existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
    if existing:
        existing.is_active = True
        existing.ogrn = ogrn
        if inn:
            existing.inn = inn
        if name:
            existing.name = name
            existing.short_name = name
        existing.rusprofile_url = rusprofile_url(ogrn)
        return existing, False
    company = Company(
        ogrn=ogrn,
        inn=inn,
        name=name,
        short_name=name,
        rusprofile_url=rusprofile_url(ogrn),
    )
    session.add(company)
    return company, True
