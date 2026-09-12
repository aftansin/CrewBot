"""Оркестрация синхронизации: лента -> разбор -> diff -> база -> уведомления."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo
from app.db.models import Event, EventStatus, Pilot, SyncStatus
from app.icalendar_feed.fetch import FeedError, FeedFetcher
from app.icalendar_feed.parse import ParseError, parse_feed
from app.icalendar_feed.types import ParsedEvent
from app.sync.differ import Diff, build_diff, guard_tripped
from app.sync.render import render_diff

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    status: SyncStatus
    diff: Diff | None = None
    error: str | None = None
    first_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status is SyncStatus.OK


class SyncService:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher: FeedFetcher,
        settings: Settings,
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._fetcher = fetcher
        self._settings = settings
        # Один пилот — одна параллельная синхронизация. Без этого двойное
        # нажатие "Обновить" рассылало дубли уведомлений.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def is_running(self, pilot_id: int) -> bool:
        return self._locks[pilot_id].locked()

    async def sync_pilot(self, pilot_id: int, notify: bool = True) -> SyncResult:
        async with self._locks[pilot_id]:
            async with self._session_factory() as session:
                try:
                    result = await self._sync(session, pilot_id, notify=notify)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
            return result

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    async def _sync(self, session: AsyncSession, pilot_id: int, notify: bool) -> SyncResult:
        pilot = await repo.get_pilot(session, pilot_id)
        if pilot is None or not pilot.ics_url:
            return SyncResult(status=SyncStatus.FETCH_ERROR, error="Ссылка не задана")

        tz = _tz_of(pilot, self._settings)

        try:
            raw = await self._fetcher.fetch(pilot.ics_url)
        except FeedError as exc:
            await repo.set_sync_result(session, pilot, SyncStatus.FETCH_ERROR, str(exc))
            return SyncResult(status=SyncStatus.FETCH_ERROR, error=str(exc))

        try:
            parsed_events, owner = parse_feed(raw, tz)
        except ParseError as exc:
            await repo.set_sync_result(session, pilot, SyncStatus.PARSE_ERROR, str(exc))
            return SyncResult(status=SyncStatus.PARSE_ERROR, error=str(exc))

        if owner and not pilot.last_name:
            await repo.update_full_name(session, pilot, owner[0], owner[1], owner[2] or None)

        stored_events = list(await repo.get_all_events(session, pilot.id))
        first_run = not stored_events
        now = datetime.now(tz=tz)

        diff = build_diff(parsed_events, stored_events, now)

        future_count = sum(
            1
            for e in stored_events
            if e.dtend >= now and e.status is EventStatus.SCHEDULED
        )
        if guard_tripped(
            diff,
            future_count,
            self._settings.cancel_guard_ratio,
            self._settings.cancel_guard_min_events,
        ):
            message = (
                f"Предохранитель: из ленты пропало {len(diff.cancelled)} из "
                f"{future_count} будущих событий. Синхронизация отменена."
            )
            logger.error("Пилот %s: %s", pilot.id, message)
            await repo.set_sync_result(session, pilot, SyncStatus.GUARD_TRIPPED, message)
            await self._alert_admins(f"\u26a0\ufe0f Пилот <code>{pilot.id}</code>\n{message}")
            return SyncResult(status=SyncStatus.GUARD_TRIPPED, error=message)

        self._apply(session, pilot, diff, now)
        await repo.set_sync_result(session, pilot, SyncStatus.OK)

        # При первой привязке календаря весь план — "новый". Сыпать сотней
        # уведомлений бессмысленно, просто загружаем молча.
        if notify and not first_run and diff.has_changes and pilot.notifications_enabled:
            await self._notify(pilot.id, diff, tz)

        return SyncResult(status=SyncStatus.OK, diff=diff, first_run=first_run)

    def _apply(self, session: AsyncSession, pilot: Pilot, diff: Diff, now: datetime) -> None:
        for parsed in diff.created:
            session.add(_build_event(pilot.id, parsed, now))

        for item in diff.updated:
            _fill_event(item.stored, item.parsed, now)

        for stored, parsed in diff.restored:
            _fill_event(stored, parsed, now)
            stored.status = EventStatus.SCHEDULED
            stored.cancelled_at = None

        for stored, parsed in diff.unchanged:
            stored.last_seen_at = now
            stored.content_hash = parsed.content_hash()

        for stored in diff.cancelled:
            stored.status = EventStatus.CANCELLED
            stored.cancelled_at = now

    async def _notify(self, chat_id: int, diff: Diff, tz: ZoneInfo) -> None:
        for message in render_diff(diff, tz):
            await self._send(chat_id, message)

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text, disable_web_page_preview=True)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            await self._bot.send_message(chat_id, text, disable_web_page_preview=True)
        except TelegramForbiddenError:
            logger.info("Пилот %s заблокировал бота", chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить сообщение пилоту %s", chat_id)

    async def _alert_admins(self, text: str) -> None:
        for admin_id in self._settings.admin_ids:
            await self._send(admin_id, text)


def _tz_of(pilot: Pilot, settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(pilot.timezone)
    except Exception:  # noqa: BLE001
        return settings.default_tz


def _build_event(pilot_id: int, parsed: ParsedEvent, now: datetime) -> Event:
    event = Event(pilot_id=pilot_id, uid=parsed.uid, first_seen_at=now)
    _fill_event(event, parsed, now)
    return event


def _fill_event(event: Event, parsed: ParsedEvent, now: datetime) -> None:
    event.kind = parsed.kind
    event.summary = parsed.summary
    event.description = parsed.description
    event.location = parsed.location
    event.dtstart = parsed.dtstart
    event.dtend = parsed.dtend
    event.flight_no = parsed.flight_no
    event.dep_code = parsed.dep_code
    event.dep_city = parsed.dep_city
    event.arr_code = parsed.arr_code
    event.arr_city = parsed.arr_city
    event.aircraft = parsed.aircraft
    event.content_hash = parsed.content_hash()
    event.last_seen_at = now
