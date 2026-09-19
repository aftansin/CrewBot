"""Точка входа."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.handlers import admin as admin_handlers
from app.bot.handlers import logbook as logbook_handlers
from app.bot.handlers import user as user_handlers
from app.bot.middlewares import DatabaseMiddleware, PilotMiddleware
from app.config import get_settings
from app.db.base import create_engine, create_session_factory
from app.icalendar_feed.fetch import FeedFetcher
from app.scheduler import PollManager
from app.sync.service import SyncService

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="plan", description="Мой план"),
    BotCommand(command="stats", description="Налёт за месяц"),
    BotCommand(command="logbook", description="Лётная книжка"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="delete", description="Удалить мои данные"),
]


def setup_logging(level: str) -> None:
    """Логи идут в stdout — их забирает docker logs.

    Старая версия слала каждый INFO админу в Telegram синхронным HTTP
    из-под асинхронного цикла. Это блокировало loop и заваливало чат.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.admin_ids:
        logger.warning("ADMIN_IDS пуст — админские функции будут недоступны")

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    fetcher = FeedFetcher(
        timeout_seconds=settings.http_timeout_seconds,
        retries=settings.http_retries,
    )
    await fetcher.start()

    sync_service = SyncService(bot, session_factory, fetcher, settings)
    scheduler = AsyncIOScheduler(timezone=settings.default_timezone)
    poll_manager = PollManager(scheduler, sync_service, session_factory, settings)

    dp = Dispatcher(
        storage=MemoryStorage(),
        settings=settings,
        sync_service=sync_service,
        poll_manager=poll_manager,
    )

    # Порядок важен: сессия -> пилот. PilotMiddleware читает data["session"].
    dp.update.outer_middleware(DatabaseMiddleware(session_factory))
    dp.update.outer_middleware(PilotMiddleware(settings))

    dp.include_router(admin_handlers.router)
    dp.include_router(logbook_handlers.router)
    dp.include_router(user_handlers.router)

    await bot.set_my_commands(BOT_COMMANDS)

    scheduler.start()
    await poll_manager.restore_all()
    logger.info("CrewBot запущен")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Останавливаюсь\u2026")
        scheduler.shutdown(wait=False)
        await fetcher.close()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")


if __name__ == "__main__":
    main()
