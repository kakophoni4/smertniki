import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import setup_dispatcher
from app.bot.middleware import DbMiddleware, RusprofileMiddleware
from app.config import settings
from app.db.session import init_db
from app.scheduler import create_scheduler
from app.services.rusprofile_client import RusprofileClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    await init_db()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    client = RusprofileClient()
    await client.start()

    dp.update.middleware(DbMiddleware())
    dp.update.middleware(RusprofileMiddleware(client))
    setup_dispatcher(dp)

    scheduler = create_scheduler(bot, client)
    scheduler.start()
    logger.info("Scheduler started: %s (%s)", settings.check_cron, settings.timezone)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
