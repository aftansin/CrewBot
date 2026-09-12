"""Загрузка ленты подписки по HTTP."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)


class FeedError(Exception):
    """Лента недоступна или ответ не похож на календарь."""


class FeedFetcher:
    """Держит один ClientSession на всё приложение.

    В старой версии сессия создавалась на каждый запрос и таймаута не было
    вовсе — одно зависшее соединение подвешивало задачу навсегда.
    """

    def __init__(self, timeout_seconds: int = 30, retries: int = 3) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retries = max(1, retries)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "text/calendar, text/plain",
                    "Accept-Language": "ru-RU",
                    "User-Agent": "CrewBot/2.0 (+calendar subscription client)",
                },
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch(self, url: str) -> str:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                async with self._session.get(url) as response:
                    if response.status != 200:
                        raise FeedError(f"HTTP {response.status}")
                    text = await response.text()
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                logger.warning("Попытка %s/%s не удалась: %s", attempt, self._retries, exc)
            except FeedError as exc:
                # 4xx повторять бессмысленно, 5xx — имеет смысл.
                last_error = exc
                if "HTTP 4" in str(exc):
                    raise
                logger.warning("Попытка %s/%s: %s", attempt, self._retries, exc)
            else:
                if "BEGIN:VCALENDAR" not in text:
                    raise FeedError("Ответ не является календарём iCalendar")
                return text

            if attempt < self._retries:
                await asyncio.sleep(2**attempt)

        raise FeedError(f"Лента недоступна после {self._retries} попыток: {last_error}")
