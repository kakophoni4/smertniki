import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db.session import SessionLocal
from app.services.monitor import check_all_companies
from app.services.rusprofile_client import RusprofileClient

logger = logging.getLogger(__name__)


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

    parts = settings.check_cron.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid CHECK_CRON: {settings.check_cron}")

    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
        timezone=settings.timezone,
    )
    scheduler.add_job(scheduled_check, trigger=trigger, id="rusprofile_check", replace_existing=True)
    return scheduler
