"""Мидлвари."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo

logger = logging.getLogger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    """Открывает сессию на апдейт и коммитит её, если хендлер не упал."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class PilotMiddleware(BaseMiddleware):
    """Кладёт в data объект пилота, создавая его при первом обращении.

    Старая версия клала в data значение, полученное ДО вставки, то есть None,
    и первый же хендлер нового пользователя падал.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            # Служебные апдейты без пользователя просто пропускаем.
            return await handler(event, data)

        session: AsyncSession = data["session"]
        pilot, created = await repo.get_or_create_pilot(
            session,
            pilot_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            default_timezone=self._settings.default_timezone,
            default_poll_interval=self._settings.poll_interval_minutes,
        )

        data["pilot"] = pilot
        data["is_new_pilot"] = created
        data["is_admin"] = self._settings.is_admin(user.id)

        if created:
            logger.info("Новый пользователь %s (@%s)", user.id, user.username)
            bot = data.get("bot")
            if bot is not None:
                for admin_id in self._settings.admin_ids:
                    if admin_id == user.id:
                        continue
                    try:
                        await bot.send_message(
                            admin_id,
                            f"\U0001f464 Новый пользователь: "
                            f"<code>{user.id}</code> @{user.username or '\u2014'}",
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("Не удалось уведомить админа %s", admin_id)

        return await handler(event, data)
