"""Экраны лётной книжки.

Поток записи рейса устроен вокруг одного наблюдения: после рейса пилот
готов напечатать ровно одну строку, а не проходить мастер из шести шагов.
Поэтому карточка показывает всё, что известно из расписания, и просит
только времена. Борт подставляется, экипаж берётся из ленты, функция
выводится из должности в задании. Всё это правится потом, если нужно.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import logbook_keyboards as lkb
from app.bot.handlers.user import _render, pilot_tz
from app.config import Settings
from app.db import logbook_repo as lrepo
from app.db.models import Function, Pilot
from app.icalendar_feed.parse import month_bounds
from app.logbook.draft import draft_from_event, resolve_airports, suggested_function
from app.logbook.tail import suggest_tail
from app.logbook.timeinput import Severity, parse_and_validate
from app.sync.render import esc, fmt_minutes

logger = logging.getLogger(__name__)
router = Router(name="logbook")


class LogStates(StatesGroup):
    waiting_for_times = State()
    waiting_for_tail_search = State()


# --------------------------------------------------------------------------
# Меню книжки
# --------------------------------------------------------------------------


@router.message(Command("logbook"))
async def cmd_logbook(
    message: Message, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    await message.answer(
        await _logbook_text(session, pilot, settings), reply_markup=lkb.logbook_menu()
    )


@router.callback_query(lkb.LogCB.filter(F.action == "menu"))
async def show_logbook(
    call: CallbackQuery, session: AsyncSession, pilot: Pilot, settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    await _render(call, await _logbook_text(session, pilot, settings), lkb.logbook_menu())
    await call.answer()


async def _logbook_text(session: AsyncSession, pilot: Pilot, settings: Settings) -> str:
    tz = pilot_tz(pilot, settings)
    now = datetime.now(tz=tz)
    start, end = month_bounds(now, tz)
    totals = await lrepo.month_totals(session, pilot.id, start.date(), end.date())
    pending = await lrepo.pending_flights(session, pilot.id, now)

    lines = [
        "\U0001f4d2 <b>Лётная книжка</b>",
        "",
        f"<b>{start:%m.%Y}</b>: {totals['flights']} рейсов, "
        f"налёт <b>{fmt_minutes(totals['block'])}</b>",
        f"КВС {fmt_minutes(totals['pic'])} · "
        f"2П {fmt_minutes(totals['copilot'])} · "
        f"ночь {fmt_minutes(totals['night'])}",
    ]
    if pending:
        lines += ["", f"\u26a0\ufe0f Не записано рейсов: <b>{len(pending)}</b>"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Незаписанные рейсы
# --------------------------------------------------------------------------


@router.callback_query(lkb.LogCB.filter(F.action == "pending"))
async def show_pending(
    call: CallbackQuery, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    tz = pilot_tz(pilot, settings)
    events = await lrepo.pending_flights(session, pilot.id, datetime.now(tz=tz))
    if not events:
        await _render(
            call,
            "\u2705 Все рейсы за последние две недели записаны.",
            lkb.back_to_logbook(),
        )
        await call.answer()
        return

    await _render(
        call,
        f"Рейсы, которых нет в книжке: <b>{len(events)}</b>\n\nВыберите, какой записать.",
        lkb.pending_keyboard(events, tz),
    )
    await call.answer()


@router.callback_query(lkb.LogCB.filter(F.action == "take"))
async def take_flight(
    call: CallbackQuery,
    callback_data: lkb.LogCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
    state: FSMContext,
) -> None:
    event = await lrepo.event_by_uid(session, pilot.id, callback_data.uid)
    if event is None:
        await call.answer("Рейс не найден", show_alert=True)
        return

    owner = await lrepo.owner_person(session, pilot.id)
    draft = draft_from_event(event, owner.last_name if owner else None)
    if draft is None:
        await call.answer("Это не рейс", show_alert=True)
        return

    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)

    await state.set_state(LogStates.waiting_for_times)
    await state.update_data(uid=event.uid, tail_id=None)

    tz = pilot_tz(pilot, settings)
    await _render(call, _draft_card(draft, tz), lkb.back_to_logbook("\u274c Отмена"))
    await call.answer()


def _draft_card(draft, tz) -> str:
    route = f"{draft.dep_icao or '????'} \u2192 {draft.arr_icao or '????'}"
    lines = [
        f"\u2708\ufe0f <b>{esc(draft.flight_number or 'рейс')}</b>  {route}",
        f"{draft.flight_date.astimezone(tz):%d.%m.%Y}",
        "",
        f"По плану: {draft.scheduled_out.astimezone(tz):%H:%M} \u2013 "
        f"{draft.scheduled_in.astimezone(tz):%H:%M}"
        if draft.scheduled_out and draft.scheduled_in
        else "Плановое время неизвестно",
    ]
    if draft.aircraft_type:
        lines.append(f"Тип: {esc(draft.aircraft_type)}")
    if draft.crew:
        lines.append("")
        lines.append("<b>Экипаж</b>")
        lines += [f"\u2022 {esc(m.display)} ({esc(m.position)})" for m in draft.crew]

    lines += [
        "",
        "<b>Пришлите фактические времена</b> запуска и выключения, "
        "в UTC, одной строкой:",
        "<code>0952 1537</code>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Ввод времён
# --------------------------------------------------------------------------


@router.message(StateFilter(LogStates.waiting_for_times), F.text)
async def receive_times(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
) -> None:
    data = await state.get_data()
    event = await lrepo.event_by_uid(session, pilot.id, data["uid"])
    if event is None:
        await message.answer("Рейс потерялся. Начните заново: /logbook")
        await state.clear()
        return

    owner = await lrepo.owner_person(session, pilot.id)
    draft = draft_from_event(event, owner.last_name if owner else None)
    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)

    out_dt, in_dt, issues = parse_and_validate(
        message.text, draft.flight_date, draft.scheduled_out, draft.scheduled_in
    )

    if out_dt is None:
        text = "\n".join(
            f"\u274c {esc(i.message)}" + (f"\n   <i>{esc(i.hint)}</i>" if i.hint else "")
            for i in issues
        )
        await message.answer(f"{text}\n\nПопробуйте ещё раз.")
        return

    draft.actual_out, draft.actual_in = out_dt, in_dt
    await state.update_data(out=out_dt.isoformat(), inn=in_dt.isoformat())

    suggestions = await suggest_tail(
        session, pilot.id, draft.flight_date, draft.dep_icao, draft.arr_icao,
        draft.scheduled_out,
    )

    lines = [
        f"\u23f1 Блок-тайм: <b>{fmt_minutes(draft.block_minutes)}</b>",
        f"Рабочее время: {fmt_minutes(draft.duty_minutes or 0)}",
    ]
    for issue in issues:
        mark = "\u26a0\ufe0f" if issue.severity == Severity.WARNING else "\u2139\ufe0f"
        lines.append(f"\n{mark} {esc(issue.message)}")
        if issue.hint:
            lines.append(f"<i>{esc(issue.hint)}</i>")

    lines += ["", "<b>Какой борт?</b>"]
    if not suggestions:
        lines.append("Подсказок нет — пришлите регистрацию или её часть.")

    await state.set_state(LogStates.waiting_for_tail_search)
    await message.answer("\n".join(lines), reply_markup=lkb.tail_keyboard(suggestions))


# --------------------------------------------------------------------------
# Выбор борта
# --------------------------------------------------------------------------


@router.message(StateFilter(LogStates.waiting_for_tail_search), F.text)
async def search_tail(message: Message, session: AsyncSession) -> None:
    found = await lrepo.search_aircraft(session, message.text.strip())
    if not found:
        await message.answer(
            "Ничего не нашлось. Попробуйте другую часть номера, "
            "например <code>73125</code> или <code>BCD</code>."
        )
        return
    await message.answer(
        f"Найдено: {len(found)}", reply_markup=lkb.tail_results_keyboard(found)
    )


@router.callback_query(lkb.TailCB.filter())
async def choose_tail(
    call: CallbackQuery,
    callback_data: lkb.TailCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if "out" not in data:
        await call.answer("Сессия истекла, начните заново", show_alert=True)
        return

    event = await lrepo.event_by_uid(session, pilot.id, data["uid"])
    owner = await lrepo.owner_person(session, pilot.id)
    draft = draft_from_event(event, owner.last_name if owner else None)
    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)
    draft.actual_out = datetime.fromisoformat(data["out"])
    draft.actual_in = datetime.fromisoformat(data["inn"])

    aircraft = await lrepo.aircraft_by_id(session, callback_data.aircraft_id)
    draft.tail = aircraft.display if aircraft else None

    function_code = suggested_function(draft)
    function = Function(function_code) if function_code else Function.UNVERIFIED

    flight = await lrepo.save_draft(
        session, pilot.id, draft, function, callback_data.aircraft_id, event
    )
    await session.commit()
    await state.clear()

    night = (
        f"ночь {fmt_minutes(flight.night_minutes)}"
        if flight.night_computed
        else "ночь не посчитана — нет координат аэропорта"
    )
    await _render(
        call,
        "\u2705 <b>Записано в книжку</b>\n\n"
        f"{esc(flight.flight_number or '')} "
        f"{flight.dep_icao}\u2192{flight.arr_icao}  "
        f"{flight.flight_date:%d.%m.%Y}\n"
        f"Борт: {esc(aircraft.display if aircraft else '\u2014')}\n"
        f"Блок-тайм <b>{fmt_minutes(flight.block_minutes)}</b>, {night}\n"
        f"Функция: {flight.function.value}\n"
        f"Рабочее время: {fmt_minutes(flight.duty_minutes or 0)}",
        lkb.after_save_keyboard(),
    )
    await call.answer("Сохранено")


# --------------------------------------------------------------------------
# Последние записи
# --------------------------------------------------------------------------


@router.callback_query(lkb.LogCB.filter(F.action == "recent"))
async def show_recent(
    call: CallbackQuery, session: AsyncSession, pilot: Pilot, settings: Settings
) -> None:
    flights = await lrepo.recent_flights(session, pilot.id, limit=12)
    if not flights:
        await _render(call, "В книжке пока пусто.", lkb.back_to_logbook())
        await call.answer()
        return

    lines = ["\U0001f4d6 <b>Последние записи</b>", ""]
    for flight in flights:
        tail = flight.aircraft.display if flight.aircraft else "\u2014"
        lines.append(
            f"{flight.flight_date:%d.%m} "
            f"{esc(flight.flight_number or ''):>6} "
            f"{flight.dep_icao}\u2192{flight.arr_icao} "
            f"{fmt_minutes(flight.block_minutes)} {esc(tail)}"
        )
    await _render(call, "\n".join(lines), lkb.back_to_logbook())
    await call.answer()
