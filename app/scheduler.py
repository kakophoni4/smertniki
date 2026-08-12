import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db.session import SessionLocal
from app.services.monitor import build_stale_ticket_nags, check_all_companies
from app.services.rusprofile_client import RusprofileClient

logger = logging.getLogger(__name__)


def _cron_trigger(expr: str) -> CronTrigger:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: {expr}")
    return CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
        timezone=settings.timezone,
    )


def create_scheduler(bot: Bot, client: RusprofileClient) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    async def scheduled_check() -> None:
        logger.info("Scheduled check started")
        from app.bot.handlers import broadcast

        async with SessionLocal() as session:
            msgs = await check_all_companies(session, client)
            if msgs:
                await broadcast(session, bot, msgs)
                logger.info("Scheduled check: %s alerts", len(msgs))
            else:
                logger.info("Scheduled check: no new alerts")

    async def scheduled_stale_nags() -> None:
        logger.info(
            "Stale ticket nag started (threshold=%s days, recipients=%s)",
            settings.stale_ticket_days,
            settings.stale_nag_id_list or "NONE",
        )
        from app.bot.handlers import broadcast_stale_nags

        async with SessionLocal() as session:
            msgs = await build_stale_ticket_nags(session)
            if not msgs:
                logger.info("Stale nag: nothing to ping")
                return
            n_recv = await broadcast_stale_nags(bot, msgs)
            logger.info("Stale nag: %s alerts → %s recipients", len(msgs), n_recv)

    scheduler.add_job(
        scheduled_check,
        trigger=_cron_trigger(settings.check_cron),
        id="rusprofile_check",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_stale_nags,
        trigger=_cron_trigger(settings.stale_nag_cron),
        id="stale_ticket_nag",
        replace_existing=True,
    )
    return scheduler
