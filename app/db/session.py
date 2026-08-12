from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _ensure_sqlite_columns(sync_conn) -> None:
    """create_all не добавляет колонки в существующие таблицы — докидываем вручную."""
    import logging

    log = logging.getLogger(__name__)
    if sync_conn.dialect.name != "sqlite":
        return
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    if "companies" in tables:
        cols = {c["name"] for c in insp.get_columns("companies")}
        for col in (
            "unreliable_address_since",
            "unreliable_director_since",
            "unreliable_founder_since",
        ):
            if col not in cols:
                sync_conn.execute(text(f"ALTER TABLE companies ADD COLUMN {col} DATETIME"))
                log.info("SQLite migrated: companies.%s", col)
    if "tickets" in tables:
        cols = {c["name"] for c in insp.get_columns("tickets")}
        if "issue_since" not in cols:
            sync_conn.execute(text("ALTER TABLE tickets ADD COLUMN issue_since DATETIME"))
            log.info("SQLite migrated: tickets.issue_since")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_sqlite_columns)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
