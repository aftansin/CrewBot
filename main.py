import asyncio
import os

from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from aiogram_dialog import setup_dialogs
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from loguru import logger
from notifiers.logging import NotificationHandler
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from db.models import Base
from dialogs.start import start_dialog
from handlers.start import start_router
from middlewares.database import DatabaseMiddleware
from middlewares.is_admin import IsAdminMiddleware
from middlewares.scheduler import SchedulerMiddleware
from middlewares.track_all_users import TrackAllUsersMiddleware
from utils.scheduler import start_all_calendar_polling

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DB = os.getenv("DB_URL")


async def main() -> None:
    engine = create_async_engine(url=DB, echo=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # Создаем таблицы, только если они не существуют
    async with engine.begin() as conn:
        # await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    dp = Dispatcher(
        db_engine=engine,
        admin_id=ADMIN_ID
    )
    dp.include_router(start_router)
    dp.include_router(start_dialog)
    setup_dialogs(dp)

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_commands = [
        BotCommand(command='start', description='Main menu'),
        BotCommand(command='help', description='Info'),
    ]
    await bot.set_my_commands(commands=bot_commands)

    # Инициализация APScheduler
    scheduler = AsyncIOScheduler()
    # Активация пользовательских таймеров
    async with sessionmaker() as session:
        await start_all_calendar_polling(bot, session, scheduler)
    scheduler.start()

    print("Including middlewares")
    dp.update.outer_middleware(IsAdminMiddleware())
    dp.update.outer_middleware(DatabaseMiddleware(sessionmaker))
    dp.update.outer_middleware(TrackAllUsersMiddleware())
    dp.update.middleware(SchedulerMiddleware(scheduler))

    await dp.start_polling(bot)


if __name__ == "__main__":
    # Конфигурируем логирование
    params = {'token': TOKEN, 'chat_id': ADMIN_ID}
    telegram_handler = NotificationHandler("telegram", defaults=params)
    logger.add(telegram_handler, level="INFO", format="{level} {message}")
    logger.add("debug.log", rotation="1 MB")
    asyncio.run(main())
