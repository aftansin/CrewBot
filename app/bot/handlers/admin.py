"""Админские хендлеры.

Фильтр по admin_ids висит на самом роутере, а не проверяется в каждом
обработчике — забыть проверку становится невозможно.
"""

from __future__ import annotations

import math
from contextlib import suppress
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.config import Settings
from app.db import repo
from app.db.models import EventKind, SyncStatus
from app.icalendar_feed.parse import month_bounds
from app.sync.render import KIND_ICON, esc, fmt_minutes
from app.sync.service import SyncService

router = Router(name="admin")

ADMIN_PAGE_SIZE = 8


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        return is_admin


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, settings: Settings) -> None:
    text, markup = await _build_list(session, settings, page=0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.AdminCB.filter())
async def admin_callbacks(
    call: CallbackQuery,
    callback_data: kb.AdminCB,
    session: AsyncSession,
    settings: Settings,
    sync_service: SyncService,
) -> None:
    if callback_data.action == "list":
        text, markup = await _build_list(session, settings, callback_data.page)
    elif callback_data.action == "pilot":
        text, markup = await _build_pilot_card(
            session, settings, callback_data.pilot_id, callback_data.page
        )
    elif callback_data.action == "sync":
        await call.answer("Синхронизирую\u2026")
        result = await sync_service.sync_pilot(callback_data.pilot_id, notify=False)
        text, markup = await _build_pilot_card(
            session, settings, callback_data.pilot_id, callback_data.page
        )
        status = "\u2705 ок" if result.ok else f"\u26a0\ufe0f {esc(result.error or '')}"
        text = f"{text}\n\n<i>Ручная синхронизация: {status}</i>"
    else:
        await call.answer()
        return

    if call.message is not None:
        # "message is not modified" при повторном нажатии — нормальная ситуация.
        with suppress(Exception):
            await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


async def _build_list(session: AsyncSession, settings: Settings, page: int):
    pilots = list(await repo.list_pilots(session))
    if not pilots:
        return "Пользователей пока нет.", kb.back_to_menu()

    total_pages = max(1, math.ceil(len(pilots) / ADMIN_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = pilots[page * ADMIN_PAGE_SIZE : (page + 1) * ADMIN_PAGE_SIZE]

    linked = sum(1 for p in pilots if p.ics_url)
    failing = sum(
        1
        for p in pilots
        if p.ics_url and p.last_sync_status not in (None, SyncStatus.OK)
    )

    text = (
        "\U0001f9d1\u200d\u2708\ufe0f <b>Пользователи</b>\n\n"
        f"Всего: <b>{len(pilots)}</b>\n"
        f"С привязанным календарём: <b>{linked}</b>\n"
        f"С ошибками синхронизации: <b>{failing}</b>"
    )
    return text, kb.admin_list_keyboard(chunk, page, total_pages)


async def _build_pilot_card(
    session: AsyncSession, settings: Settings, pilot_id: int, page: int
):
    pilot = await repo.get_pilot(session, pilot_id)
    if pilot is None:
        return "Пользователь не найден.", kb.back_to_menu()

    try:
        tz = ZoneInfo(pilot.timezone)
    except Exception:  # noqa: BLE001
        tz = settings.default_tz
    now = datetime.now(tz=tz)

    lines = [
        f"\U0001f464 <b>{esc(pilot.display_name)}</b>",
        f"ID: <code>{pilot.id}</code>",
        f"Username: @{esc(pilot.username) if pilot.username else '\u2014'}",
        f"Регистрация: {pilot.created_at.astimezone(tz):%d.%m.%Y}",
        "",
        f"Календарь: {'\u2705 привязан' if pilot.ics_url else '\u26aa нет'}",
    ]

    if pilot.last_sync_at:
        mark = "\u2705" if pilot.last_sync_status is SyncStatus.OK else "\u26a0\ufe0f"
        lines.append(
            f"Проверка: {mark} {pilot.last_sync_at.astimezone(tz):%d.%m %H:%M}"
        )
        if pilot.last_sync_error:
            lines.append(f"<code>{esc(pilot.last_sync_error[:200])}</code>")
    lines.append(f"Интервал опроса: {pilot.poll_interval_minutes} мин")

    lines.append("")
    lines.append("<b>Налёт</b>")
    for offset, label in ((-1, "Прошлый"), (0, "Текущий"), (1, "Следующий")):
        start, end = month_bounds(now, tz, offset)
        minutes = await repo.get_flight_minutes(session, pilot.id, start, end)
        lines.append(f"{label} ({start:%m.%Y}): {fmt_minutes(minutes)}")

    start, end = month_bounds(now, tz)
    events = await repo.get_events_in_range(session, pilot.id, start, end)
    if events:
        by_kind: dict[EventKind, int] = {}
        for event in events:
            by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
        lines.append("")
        lines.append(f"<b>План на {start:%m.%Y}</b> — {len(events)} событий")
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            lines.append(f"{KIND_ICON.get(kind, '')} {kind.value}: {count}")

        upcoming = await repo.get_upcoming_events(session, pilot.id, now, limit=5)
        if upcoming:
            lines.append("")
            lines.append("<b>Ближайшее</b>")
            for event in upcoming:
                icon = KIND_ICON.get(event.kind, "")
                local = event.dtstart.astimezone(tz)
                title = " ".join(event.summary.split())[:38]
                lines.append(f"{icon} {local:%d.%m %H:%M} {esc(title)}")

    return "\n".join(lines), kb.admin_pilot_keyboard(pilot.id, page)


class AdminBroadcastGuard(BaseMiddleware):
    """Заглушка под будущую рассылку. Оставлена, чтобы не плодить роутеры."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        return await handler(event, data)
