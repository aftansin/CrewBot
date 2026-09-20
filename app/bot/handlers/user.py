"""Пользовательские хендлеры."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.config import Settings
from app.db import repo
from app.db.models import Pilot, SyncStatus
from app.icalendar_feed.parse import month_bounds
from app.scheduler import PollManager
from app.sync.render import (
    KIND_ICON,
    KIND_LABEL,
    esc,
    event_card,
    fmt_minutes,
    render_diff,
)
from app.sync.service import SyncService

logger = logging.getLogger(__name__)
router = Router(name="user")


class LinkStates(StatesGroup):
    waiting_for_url = State()


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def pilot_tz(pilot: Pilot, settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(pilot.timezone)
    except Exception:  # noqa: BLE001
        return settings.default_tz


async def show_menu(target: Message | CallbackQuery, pilot: Pilot, is_admin: bool) -> None:
    greeting = pilot.display_name
    if pilot.ics_url:
        text = (
            f"\U0001f6eb <b>CrewBot</b>\n\n"
            f"{esc(greeting)}\n"
            f"\u2705 Календарь привязан\n"
            f"{_sync_status_line(pilot)}"
        )
    else:
        text = (
            "\U0001f6eb <b>CrewBot</b>\n\n"
            "\u274c Календарь не привязан\n\n"
            "Нажмите «Привязать календарь» и пришлите ссылку подписки."
        )
    markup = kb.main_menu(has_link=bool(pilot.ics_url), is_admin=is_admin)
    await _render(target, text, markup)


def _sync_status_line(pilot: Pilot) -> str:
    if pilot.last_sync_at is None:
        return "Последняя проверка: ещё не было."
    stamp = pilot.last_sync_at.astimezone(ZoneInfo(pilot.timezone)).strftime("%d.%m %H:%M")
    if pilot.last_sync_status is SyncStatus.OK:
        return f"Последняя проверка: {stamp}"
    return f"Последняя проверка: {stamp} \u26a0\ufe0f {esc(pilot.last_sync_error or '')}"


async def _render(target: Message | CallbackQuery, text: str, markup=None) -> None:
    """Редактирует сообщение для callback и отправляет новое для команды."""
    if isinstance(target, CallbackQuery):
        if target.message is None:
            return
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            # "message is not modified" — нормальная ситуация при повторном нажатии.
            if "message is not modified" not in str(exc):
                await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message, pilot: Pilot, is_admin: bool, is_new_pilot: bool, state: FSMContext
) -> None:
    await state.clear()
    if is_new_pilot:
        await message.answer(
            "\U0001f44b Привет! Я слежу за вашим рабочим планом.\n\n"
            "Пришлите ссылку подписки на календарь — дальше я буду сам "
            "проверять её и сообщать об изменениях."
        )
    await show_menu(message, pilot, is_admin)


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(
        "<b>Что умеет бот</b>\n\n"
        "\u2022 Следит за подписным календарём и присылает новые события, "
        "изменения времени, смены экипажа и отмены.\n"
        "\u2022 Считает налёт за месяц.\n"
        "\u2022 Показывает план на ближайшее время.\n\n"
        "<b>Команды</b>\n"
        "/start — меню\n"
        "/plan — план\n"
        "/stats — налёт\n"
        "/settings — интервал проверки и уведомления\n"
        "/delete — удалить все свои данные\n\n"
        f"Ссылка должна начинаться с <code>{esc(settings.ics_url_prefix)}</code>",
        reply_markup=kb.back_to_menu(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, pilot: Pilot, is_admin: bool) -> None:
    await show_menu(message, pilot, is_admin)


# --------------------------------------------------------------------------
# Привязка календаря
# --------------------------------------------------------------------------


@router.callback_query(kb.MenuCB.filter(F.action == "link"))
async def ask_for_url(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.set_state(LinkStates.waiting_for_url)
    await _render(
        call,
        "Пришлите ссылку подписки на календарь.\n\n"
        f"Она должна начинаться с <code>{esc(settings.ics_url_prefix)}</code>\n\n"
        "Отменить — /menu",
        kb.back_to_menu("\u274c Отмена"),
    )
    await call.answer()


@router.message(StateFilter(LinkStates.waiting_for_url), F.text)
async def receive_url(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    pilot: Pilot,
    is_admin: bool,
    settings: Settings,
    sync_service: SyncService,
    poll_manager: PollManager,
) -> None:
    url = (message.text or "").strip()
    if not url.startswith(settings.ics_url_prefix):
        await message.answer(
            "\u274c Не похоже на ссылку подписки.\n"
            f"Ожидается адрес, начинающийся с <code>{esc(settings.ics_url_prefix)}</code>"
        )
        return

    await repo.set_ics_url(session, pilot, url)
    await session.commit()
    await state.clear()

    status = await message.answer("\u23f3 Проверяю ссылку и загружаю план\u2026")
    result = await sync_service.sync_pilot(pilot.id, notify=False)

    if not result.ok:
        await repo.set_ics_url(session, pilot, None)
        await session.commit()
        await status.edit_text(
            "\u274c Не удалось загрузить календарь.\n"
            f"<code>{esc(result.error or 'неизвестная ошибка')}</code>\n\n"
            "Проверьте ссылку и попробуйте ещё раз.",
            reply_markup=kb.back_to_menu(),
        )
        return

    poll_manager.schedule(pilot.id, pilot.poll_interval_minutes)
    await session.refresh(pilot)
    loaded = await repo.count_events(session, pilot.id)
    await status.edit_text(
        f"\u2705 Календарь привязан. Загружено событий: <b>{loaded}</b>.\n"
        f"Буду проверять план каждые {pilot.poll_interval_minutes} мин "
        "и присылать изменения."
    )
    await show_menu(message, pilot, is_admin)


# --------------------------------------------------------------------------
# Меню и обновление
# --------------------------------------------------------------------------


@router.callback_query(kb.MenuCB.filter(F.action == "main"))
async def back_to_main(call: CallbackQuery, pilot: Pilot, is_admin: bool, state: FSMContext) -> None:
    await state.clear()
    await show_menu(call, pilot, is_admin)
    await call.answer()


@router.callback_query(kb.MenuCB.filter(F.action == "noop"))
async def noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(kb.MenuCB.filter(F.action == "help"))
async def menu_help(call: CallbackQuery, settings: Settings) -> None:
    await _render(
        call,
        "Ссылку подписки можно взять в личном кабинете экипажа — "
        "раздел с календарём, кнопка подписки.\n\n"
        f"Ожидается адрес вида <code>{esc(settings.ics_url_prefix)}\u2026</code>",
        kb.back_to_menu(),
    )
    await call.answer()


@router.callback_query(kb.MenuCB.filter(F.action == "refresh"))
async def refresh(
    call: CallbackQuery,
    pilot: Pilot,
    is_admin: bool,
    settings: Settings,
    sync_service: SyncService,
) -> None:
    if not pilot.ics_url:
        await call.answer("Календарь не привязан", show_alert=True)
        return

    if sync_service.is_running(pilot.id):
        await call.answer("Проверка уже идёт, подождите", show_alert=True)
        return

    await call.answer("Проверяю\u2026")
    result = await sync_service.sync_pilot(pilot.id, notify=False)
    tz = pilot_tz(pilot, settings)

    if not result.ok:
        await _render(
            call,
            f"\u26a0\ufe0f Не удалось обновить план.\n<code>{esc(result.error or '')}</code>",
            kb.back_to_menu(),
        )
        return

    diff = result.diff
    if diff is None or not diff.has_changes:
        # Старый бот в этом случае молчал, и было непонятно, сработало ли.
        await _render(
            call,
            "\u2705 План проверен. Изменений нет.",
            kb.main_menu(has_link=True, is_admin=is_admin),
        )
        return

    messages = render_diff(diff, tz)
    header = f"\u2705 Найдено изменений: <b>{diff.total_changes}</b>"
    await _render(call, header, None)
    for text in messages:
        await call.message.answer(text, disable_web_page_preview=True)
    await call.message.answer(
        "Что дальше?", reply_markup=kb.main_menu(has_link=True, is_admin=is_admin)
    )


# --------------------------------------------------------------------------
# План
# --------------------------------------------------------------------------


@router.message(Command("plan"))
async def cmd_plan(
    message: Message, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    text, markup = await _build_plan(session, pilot, settings, page=0, scope="upcoming")
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.PlanCB.filter())
async def show_plan(
    call: CallbackQuery,
    callback_data: kb.PlanCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
) -> None:
    text, markup = await _build_plan(
        session, pilot, settings, page=callback_data.page, scope=callback_data.scope
    )
    await _render(call, text, markup)
    await call.answer()


async def _build_plan(
    session: AsyncSession, pilot: Pilot, settings: Settings, page: int, scope: str
):
    tz = pilot_tz(pilot, settings)
    now = datetime.now(tz=tz)

    if scope == "month":
        start, end = month_bounds(now, tz)
        events = list(await repo.get_events_in_range(session, pilot.id, start, end))
        title = f"\U0001f5d3 <b>План на {now:%m.%Y}</b>"
    else:
        events = list(
            await repo.get_upcoming_events(session, pilot.id, now - timedelta(hours=3))
        )
        title = "\u23ed <b>Предстоящие события</b>"

    if not events:
        return (
            f"{title}\n\nПусто. Попробуйте обновить план.",
            kb.back_to_menu(),
        )

    total_pages = max(1, math.ceil(len(events) / kb.PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = events[page * kb.PAGE_SIZE : (page + 1) * kb.PAGE_SIZE]

    text = f"{title}\n\nВсего событий: {len(events)}\nВыберите событие для подробностей."
    return text, kb.plan_keyboard(chunk, page, total_pages, scope, tz)


@router.callback_query(kb.EventCB.filter())
async def show_event(
    call: CallbackQuery,
    callback_data: kb.EventCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
) -> None:
    event = await repo.get_event(session, pilot.id, callback_data.event_id)
    if event is None:
        await call.answer("Событие не найдено", show_alert=True)
        return
    tz = pilot_tz(pilot, settings)
    await _render(
        call,
        event_card(event, tz),
        kb.event_keyboard(callback_data.page, callback_data.scope),
    )
    await call.answer()


# --------------------------------------------------------------------------
# Налёт
# --------------------------------------------------------------------------


@router.message(Command("stats"))
async def cmd_stats(
    message: Message, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    await message.answer(
        await build_stats_text(session, pilot, settings), reply_markup=kb.back_to_menu()
    )


@router.callback_query(kb.MenuCB.filter(F.action == "stats"))
async def show_stats(
    call: CallbackQuery, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    await _render(call, await build_stats_text(session, pilot, settings), kb.back_to_menu())
    await call.answer()


async def build_stats_text(session: AsyncSession, pilot: Pilot, settings: Settings) -> str:
    tz = pilot_tz(pilot, settings)
    now = datetime.now(tz=tz)

    from app.db import logbook_repo as lrepo

    lines = ["\U0001f4ca <b>Налёт</b>", ""]
    lines.append("<b>По расписанию</b> <i>(из календаря)</i>")
    for offset, label in ((-1, "Прошлый"), (0, "Текущий"), (1, "Следующий")):
        start, end = month_bounds(now, tz, offset)
        minutes = await repo.get_flight_minutes(session, pilot.id, start, end)
        value = fmt_minutes(minutes)
        row = f"{label} ({start:%m.%Y}): "
        row += f"<b>{value}</b>" if offset == 0 else value
        lines.append(row)

    # Те же месяцы по книжке. Расписание показывает только текущий и
    # следующий месяц, прошлый из ленты уже пропал — отсюда нули выше.
    # Книжка помнит всё, поэтому цифры рядом и нужны.
    lines.append("")
    lines.append("<b>По книжке</b> <i>(фактический)</i>")
    for offset, label in ((-1, "Прошлый"), (0, "Текущий")):
        start, end = month_bounds(now, tz, offset)
        totals = await lrepo.month_totals(session, pilot.id, start.date(), end.date())
        value = fmt_minutes(totals["block"])
        row = f"{label} ({start:%m.%Y}): "
        row += f"<b>{value}</b>" if offset == 0 else value
        if totals["night"]:
            row += f"   ночь {fmt_minutes(totals['night'])}"
        lines.append(row)

    start, end = month_bounds(now, tz)
    events = await repo.get_events_in_range(session, pilot.id, start, end)
    by_kind: dict = {}
    for event in events:
        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1

    if by_kind:
        lines.append("")
        lines.append(f"<b>Состав месяца {start:%m.%Y}</b> <i>(по расписанию)</i>")
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            icon = KIND_ICON.get(kind, "")
            lines.append(f"{icon} {KIND_LABEL.get(kind, kind.value)}: {count}")

    lines.append("")
    lines.append(
        "<i>Расписание показывает текущий и следующий месяц, "
        "прошлый из ленты уже пропал. Книжка помнит всё.</i>"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


@router.message(Command("settings"))
async def cmd_settings(message: Message, pilot: Pilot) -> None:
    await message.answer(_settings_text(pilot), reply_markup=_settings_markup(pilot))


@router.callback_query(kb.MenuCB.filter(F.action == "settings"))
async def show_settings(call: CallbackQuery, pilot: Pilot) -> None:
    await _render(call, _settings_text(pilot), _settings_markup(pilot))
    await call.answer()


def _settings_text(pilot: Pilot) -> str:
    return (
        "\u2699\ufe0f <b>Настройки</b>\n\n"
        f"Интервал проверки: <b>{pilot.poll_interval_minutes} мин</b>\n"
        f"Таймзона: <b>{esc(pilot.timezone)}</b>\n\n"
        "Выберите, как часто проверять календарь."
    )


def _settings_markup(pilot: Pilot):
    return kb.settings_keyboard(pilot.poll_interval_minutes, pilot.notifications_enabled)


@router.callback_query(kb.IntervalCB.filter())
async def set_interval(
    call: CallbackQuery,
    callback_data: kb.IntervalCB,
    session: AsyncSession,
    pilot: Pilot,
    poll_manager: PollManager,
) -> None:
    await repo.set_poll_interval(session, pilot, callback_data.minutes)
    await session.commit()
    if pilot.ics_url:
        poll_manager.schedule(pilot.id, pilot.poll_interval_minutes)
    await _render(call, _settings_text(pilot), _settings_markup(pilot))
    await call.answer(f"Интервал: {callback_data.minutes} мин")


@router.callback_query(kb.MenuCB.filter(F.action == "toggle_notify"))
async def toggle_notify(call: CallbackQuery, session: AsyncSession, pilot: Pilot) -> None:
    pilot.notifications_enabled = not pilot.notifications_enabled
    await session.commit()
    await _render(call, _settings_text(pilot), _settings_markup(pilot))
    await call.answer("Уведомления включены" if pilot.notifications_enabled else "Уведомления выключены")


@router.callback_query(kb.MenuCB.filter(F.action == "unlink"))
async def ask_unlink(call: CallbackQuery) -> None:
    await _render(
        call,
        "Отвязать календарь?\n\n"
        "Ссылка будет удалена, проверки остановятся. "
        "История событий сохранится.",
        kb.confirm_keyboard("unlink"),
    )
    await call.answer()


@router.callback_query(kb.ConfirmCB.filter(F.action == "unlink"))
async def do_unlink(
    call: CallbackQuery,
    callback_data: kb.ConfirmCB,
    session: AsyncSession,
    pilot: Pilot,
    is_admin: bool,
    poll_manager: PollManager,
) -> None:
    if callback_data.yes:
        await repo.set_ics_url(session, pilot, None)
        await session.commit()
        poll_manager.unschedule(pilot.id)
        await call.answer("Календарь отвязан")
    else:
        await call.answer("Отменено")
    await show_menu(call, pilot, is_admin)


# --------------------------------------------------------------------------
# Удаление данных
# --------------------------------------------------------------------------


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    await message.answer(
        "\u26a0\ufe0f Удалить все ваши данные?\n\n"
        "Будут стёрты ссылка, весь сохранённый план и история налёта. "
        "Отменить это будет нельзя.",
        reply_markup=kb.confirm_keyboard("delete"),
    )


@router.callback_query(kb.ConfirmCB.filter(F.action == "delete"))
async def do_delete(
    call: CallbackQuery,
    callback_data: kb.ConfirmCB,
    session: AsyncSession,
    pilot: Pilot,
    is_admin: bool,
    poll_manager: PollManager,
) -> None:
    if not callback_data.yes:
        await call.answer("Отменено")
        await show_menu(call, pilot, is_admin)
        return

    poll_manager.unschedule(pilot.id)
    await repo.delete_pilot(session, pilot)
    await session.commit()
    await _render(call, "Данные удалены. Отправьте /start, чтобы начать заново.", None)
    await call.answer()
