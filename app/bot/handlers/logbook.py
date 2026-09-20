"""Экраны лётной книжки.

Поток записи рейса устроен вокруг одного наблюдения: после рейса пилот
готов напечатать ровно одну строку, а не проходить мастер из шести шагов.
Поэтому карточка показывает всё, что известно из расписания, и просит
только времена. Борт подставляется, экипаж берётся из ленты, функция
выводится из должности в задании. Всё это правится потом, если нужно.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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
from app.sync.render import esc, fmt_minutes, function_label

logger = logging.getLogger(__name__)
router = Router(name="logbook")


class LogStates(StatesGroup):
    waiting_for_times = State()
    waiting_for_tail_search = State()
    waiting_for_function = State()
    # Правка уже сохранённой записи
    editing_times = State()
    editing_tail = State()
    editing_remarks = State()


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
        f"<b>{start:%m.%Y}</b>",
        f"Налёт: <b>{fmt_minutes(totals['block'])}</b>   "
        f"ночь: <b>{fmt_minutes(totals['night'])}</b>",
        f"Рейсов: {totals['flights']}",
    ]
    if pending:
        lines += ["", f"\u26a0\ufe0f Не записано рейсов: <b>{len(pending)}</b>"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Незаписанные рейсы
# --------------------------------------------------------------------------


@router.callback_query(lkb.LogCB.filter(F.action == "noop"))
async def logbook_noop(call: CallbackQuery) -> None:
    """Счётчик страниц — не кнопка, но Telegram ждёт ответа на нажатие.

    Без этого обработчика нажатие на "1/390" висит до таймаута.
    """
    await call.answer()


@router.callback_query(lkb.LogCB.filter(F.action == "pending"))
async def show_pending(
    call: CallbackQuery,
    callback_data: lkb.LogCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
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

    page = callback_data.page
    await _render(
        call,
        f"Рейсы, которых нет в книжке: <b>{len(events)}</b>\n\nВыберите, какой записать.",
        lkb.pending_keyboard(events, tz, page),
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
    draft = draft_from_event(
        event,
        owner.last_name if owner else None,
        owner.first_name if owner else None,
    )
    if draft is None:
        await call.answer("Это не рейс", show_alert=True)
        return

    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)

    await state.set_state(LogStates.waiting_for_times)
    await state.update_data(uid=event.uid, tail_id=None)

    tz = pilot_tz(pilot, settings)
    await _render(call, _draft_card(draft, tz), lkb.draft_keyboard(event.uid))
    await call.answer()


@router.callback_query(lkb.LogCB.filter(F.action == "skip"))
async def skip_flight(
    call: CallbackQuery,
    callback_data: lkb.LogCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Рейса не было: план поменяли задним числом.

    Событие помечается отменённым, а не удаляется. Если оно вернётся
    в ленту, синхронизация восстановит статус.
    """
    await lrepo.dismiss_event(session, pilot.id, callback_data.uid)
    await session.commit()
    await state.clear()
    await _render(
        call,
        "\U0001f6ab Отмечено: рейс не выполнялся.\n"
        "<i>Предлагать его больше не буду.</i>",
        lkb.after_save_keyboard(),
    )
    await call.answer("Убрано из списка")


def _draft_card(draft, tz) -> str:
    """Плановое время показывается в двух поясах.

    Расписание приходит московским, книжка ведётся в UTC. Показывать
    только одно из них — верный способ однажды записать не то время,
    поэтому видны оба, а ввод явно помечен как UTC.
    """
    route = f"{draft.dep_icao or '????'} \u2192 {draft.arr_icao or '????'}"
    lines = [
        f"\u2708\ufe0f <b>{esc(draft.flight_number or 'рейс')}</b>  {route}",
        f"{draft.flight_date.astimezone(tz):%d.%m.%Y}",
        "",
    ]
    if draft.scheduled_out and draft.scheduled_in:
        local_out = draft.scheduled_out.astimezone(tz)
        local_in = draft.scheduled_in.astimezone(tz)
        utc_out = draft.scheduled_out.astimezone(UTC)
        utc_in = draft.scheduled_in.astimezone(UTC)
        lines += [
            f"По плану <b>{local_out:%H:%M}\u2013{local_in:%H:%M} МСК</b>",
            f"            {utc_out:%H:%M}\u2013{utc_in:%H:%M} UTC",
        ]
    else:
        lines.append("Плановое время неизвестно")
    if draft.aircraft_type:
        lines.append(f"Тип: {esc(draft.aircraft_type)}")
    if draft.crew:
        lines.append("")
        lines.append("<b>Экипаж</b>")
        lines += [f"\u2022 {esc(m.display)} ({esc(m.position)})" for m in draft.crew]

    lines += [
        "",
        "<b>Пришлите фактические времена</b> запуска и выключения "
        "\u2014 <u>в UTC</u>, одной строкой:",
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
    draft = draft_from_event(
        event,
        owner.last_name if owner else None,
        owner.first_name if owner else None,
    )
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
    draft = draft_from_event(
        event,
        owner.last_name if owner else None,
        owner.first_name if owner else None,
    )
    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)
    draft.actual_out = datetime.fromisoformat(data["out"])
    draft.actual_in = datetime.fromisoformat(data["inn"])

    aircraft = await lrepo.aircraft_by_id(session, callback_data.aircraft_id)
    draft.tail = aircraft.display if aircraft else None

    function_code = suggested_function(draft)
    if function_code is None:
        # Должность в задании не распозналась — спрашиваем, а не пишем
        # в книжку "функция не указана". Это лётный документ.
        await state.update_data(aircraft_id=callback_data.aircraft_id)
        await state.set_state(LogStates.waiting_for_function)
        await _render(
            call,
            "В каком качестве вы выполняли рейс?\n\n"
            "<i>Определить по заданию не удалось.</i>",
            lkb.function_keyboard(),
        )
        await call.answer()
        return

    flight = await _persist(
        session, pilot, draft, Function(function_code), callback_data.aircraft_id, event
    )
    await state.clear()

    await _render(call, _saved_card(flight, aircraft), lkb.after_save_keyboard())
    await call.answer("Сохранено")


async def _persist(session, pilot, draft, function, aircraft_id, event):
    flight = await lrepo.save_draft(
        session, pilot.id, draft, function, aircraft_id, event
    )
    await session.commit()
    return flight


def _saved_card(flight, aircraft) -> str:
    night = (
        f"ночь {fmt_minutes(flight.night_minutes)}"
        if flight.night_computed
        else "ночь не посчитана \u2014 нет координат аэропорта"
    )
    tail = aircraft.display if aircraft else "\u2014"
    return (
        "\u2705 <b>Записано в книжку</b>\n\n"
        f"{esc(flight.flight_number or '')} "
        f"{flight.dep_icao}\u2192{flight.arr_icao}  "
        f"{flight.flight_date:%d.%m.%Y}\n"
        f"Борт: {esc(tail)}\n"
        f"Блок-тайм <b>{fmt_minutes(flight.block_minutes)}</b>, {night}\n"
        f"Функция: <b>{esc(function_label(flight.function))}</b>\n"
        f"Рабочее время: {fmt_minutes(flight.duty_minutes or 0)}"
    )


@router.callback_query(lkb.FuncCB.filter())
async def choose_function(
    call: CallbackQuery,
    callback_data: lkb.FuncCB,
    session: AsyncSession,
    pilot: Pilot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if "out" not in data:
        await call.answer("Сессия истекла, начните заново", show_alert=True)
        return

    event = await lrepo.event_by_uid(session, pilot.id, data["uid"])
    owner = await lrepo.owner_person(session, pilot.id)
    draft = draft_from_event(
        event,
        owner.last_name if owner else None,
        owner.first_name if owner else None,
    )
    mapping = await lrepo.iata_to_icao_map(session, [draft.dep_icao, draft.arr_icao])
    resolve_airports(draft, mapping)
    draft.actual_out = datetime.fromisoformat(data["out"])
    draft.actual_in = datetime.fromisoformat(data["inn"])

    aircraft_id = data.get("aircraft_id")
    aircraft = await lrepo.aircraft_by_id(session, aircraft_id) if aircraft_id else None

    flight = await _persist(
        session, pilot, draft, Function(callback_data.value), aircraft_id, event
    )
    await state.clear()
    await _render(call, _saved_card(flight, aircraft), lkb.after_save_keyboard())
    await call.answer("Сохранено")


# --------------------------------------------------------------------------
# Последние записи
# --------------------------------------------------------------------------


@router.callback_query(lkb.LogCB.filter(F.action == "recent"))
async def show_recent(
    call: CallbackQuery,
    callback_data: lkb.LogCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
) -> None:
    page = callback_data.page
    total = await lrepo.count_flights(session, pilot.id)
    if not total:
        await _render(call, "В книжке пока пусто.", lkb.back_to_logbook())
        await call.answer()
        return

    pages = max(1, (total + lkb.PAGE_SIZE - 1) // lkb.PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    flights = await lrepo.recent_flights(
        session, pilot.id, limit=lkb.PAGE_SIZE, offset=page * lkb.PAGE_SIZE
    )

    lines = [f"\U0001f4d6 <b>Записи</b>  ({total} всего)", ""]
    for flight in flights:
        tail = flight.aircraft.display if flight.aircraft else "\u2014"
        night = f"  \U0001f319 {fmt_minutes(flight.night_minutes)}" if flight.night_minutes else ""
        lines.append(
            f"{flight.flight_date:%d.%m.%y} "
            f"{flight.dep_icao}\u2192{flight.arr_icao} "
            f"<b>{fmt_minutes(flight.block_minutes)}</b>{night}  {esc(tail)}"
        )
    lines.append("")
    lines.append("<i>Нажмите на запись, чтобы посмотреть или поправить.</i>")
    await _render(call, "\n".join(lines), lkb.recent_entry_keyboard(flights, page, pages))
    await call.answer()



# --------------------------------------------------------------------------
# Правка сохранённой записи
# --------------------------------------------------------------------------


def _flight_card(flight, tz) -> str:
    tail = flight.aircraft.display if flight.aircraft else "\u2014"
    lines = [
        f"\u2708\ufe0f <b>{esc(flight.flight_number or 'рейс')}</b>  "
        f"{flight.dep_icao}\u2192{flight.arr_icao}",
        f"{flight.flight_date:%d.%m.%Y}",
        "",
    ]
    if flight.out_utc and flight.in_utc:
        lines.append(
            f"Запуск \u2013 выключение: "
            f"<b>{flight.out_utc:%H:%M}\u2013{flight.in_utc:%H:%M} UTC</b>"
        )
    lines += [
        f"Блок-тайм: <b>{fmt_minutes(flight.block_minutes)}</b>",
        f"Ночь: {fmt_minutes(flight.night_minutes)}"
        + ("" if flight.night_computed else "  <i>(не рассчитано)</i>"),
        f"Борт: <b>{esc(tail)}</b>",
        f"Функция: <b>{esc(function_label(flight.function))}</b>",
    ]
    if flight.duty_minutes:
        lines.append(f"Рабочее время: {fmt_minutes(flight.duty_minutes)}")
    if flight.remarks:
        lines.append("")
        lines.append(f"\U0001f4dd {esc(flight.remarks)}")
    return "\n".join(lines)


@router.callback_query(lkb.EditCB.filter(F.action == "open"))
async def open_flight(
    call: CallbackQuery,
    callback_data: lkb.EditCB,
    session: AsyncSession,
    pilot: Pilot,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    flight = await lrepo.get_flight(session, pilot.id, callback_data.flight_id)
    if flight is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    tz = pilot_tz(pilot, settings)
    await _render(
        call,
        _flight_card(flight, tz),
        lkb.flight_card_keyboard(flight.id, callback_data.page),
    )
    await call.answer()


@router.callback_query(lkb.EditCB.filter(F.action == "times"))
async def ask_new_times(
    call: CallbackQuery, callback_data: lkb.EditCB, session: AsyncSession, pilot: Pilot,
    state: FSMContext,
) -> None:
    flight = await lrepo.get_flight(session, pilot.id, callback_data.flight_id)
    if flight is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(LogStates.editing_times)
    await state.update_data(flight_id=flight.id, page=callback_data.page)
    current = (
        f"{flight.out_utc:%H%M} {flight.in_utc:%H%M}"
        if flight.out_utc and flight.in_utc
        else "0952 1537"
    )
    await _render(
        call,
        "Пришлите новые времена запуска и выключения \u2014 <u>в UTC</u>:\n"
        f"<code>{current}</code>\n\n"
        "<i>Блок-тайм и ночное время пересчитаются.</i>",
        lkb.edit_cancel_keyboard(flight.id, callback_data.page),
    )
    await call.answer()


@router.message(StateFilter(LogStates.editing_times), F.text)
async def apply_new_times(
    message: Message, state: FSMContext, session: AsyncSession, pilot: Pilot,
    settings: Settings,
) -> None:
    data = await state.get_data()
    flight = await lrepo.get_flight(session, pilot.id, data["flight_id"])
    if flight is None:
        await message.answer("Запись потерялась. Откройте её заново: /logbook")
        await state.clear()
        return

    flight_date = datetime.combine(
        flight.flight_date, datetime.min.time(), tzinfo=UTC
    )
    out_dt, in_dt, issues = parse_and_validate(message.text, flight_date)
    if out_dt is None:
        text = "\n".join(
            f"\u274c {esc(i.message)}" + (f"\n   <i>{esc(i.hint)}</i>" if i.hint else "")
            for i in issues
        )
        await message.answer(f"{text}\n\nПопробуйте ещё раз.")
        return

    await lrepo.edit_times(session, flight, out_dt, in_dt)
    await session.commit()
    await state.clear()

    tz = pilot_tz(pilot, settings)
    warn = "".join(
        f"\n\u26a0\ufe0f {esc(i.message)}" for i in issues if i.severity == Severity.WARNING
    )
    await message.answer(
        f"\u2705 <b>Изменено</b>{warn}\n\n{_flight_card(flight, tz)}",
        reply_markup=lkb.flight_card_keyboard(flight.id, data.get("page", 0)),
    )


@router.callback_query(lkb.EditCB.filter(F.action == "aircraft"))
async def ask_new_tail(
    call: CallbackQuery, callback_data: lkb.EditCB, state: FSMContext
) -> None:
    await state.set_state(LogStates.editing_tail)
    await state.update_data(flight_id=callback_data.flight_id, page=callback_data.page)
    await _render(
        call,
        "Пришлите регистрацию борта или её часть:\n"
        "<code>73125</code>  или  <code>BCD</code>",
        lkb.edit_cancel_keyboard(callback_data.flight_id, callback_data.page),
    )
    await call.answer()


@router.message(StateFilter(LogStates.editing_tail), F.text)
async def search_new_tail(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    found = await lrepo.search_aircraft(session, message.text.strip())
    if not found:
        await message.answer("Ничего не нашлось. Попробуйте другую часть номера.")
        return
    await message.answer(
        f"Найдено: {len(found)}",
        reply_markup=lkb.edit_tail_keyboard(found, data["flight_id"], data.get("page", 0)),
    )


@router.callback_query(lkb.TailEditCB.filter())
async def apply_new_tail(
    call: CallbackQuery, callback_data: lkb.TailEditCB, session: AsyncSession,
    pilot: Pilot, settings: Settings, state: FSMContext,
) -> None:
    flight = await lrepo.get_flight(session, pilot.id, callback_data.flight_id)
    if flight is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await lrepo.edit_aircraft(session, flight, callback_data.aircraft_id)
    await session.commit()
    await state.clear()
    tz = pilot_tz(pilot, settings)
    await _render(
        call,
        f"\u2705 <b>Борт изменён</b>\n\n{_flight_card(flight, tz)}",
        lkb.flight_card_keyboard(flight.id, callback_data.page),
    )
    await call.answer("Изменено")


@router.callback_query(lkb.EditCB.filter(F.action == "function"))
async def ask_new_function(call: CallbackQuery, callback_data: lkb.EditCB) -> None:
    await _render(
        call,
        "В каком качестве выполнялся рейс?",
        lkb.edit_function_keyboard(callback_data.flight_id, callback_data.page),
    )
    await call.answer()


@router.callback_query(lkb.FuncEditCB.filter())
async def apply_new_function(
    call: CallbackQuery, callback_data: lkb.FuncEditCB, session: AsyncSession,
    pilot: Pilot, settings: Settings,
) -> None:
    flight = await lrepo.get_flight(session, pilot.id, callback_data.flight_id)
    if flight is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await lrepo.edit_function(session, flight, Function(callback_data.value))
    await session.commit()
    tz = pilot_tz(pilot, settings)
    await _render(
        call,
        f"\u2705 <b>Функция изменена</b>\n\n{_flight_card(flight, tz)}",
        lkb.flight_card_keyboard(flight.id, callback_data.page),
    )
    await call.answer("Изменено")


@router.callback_query(lkb.EditCB.filter(F.action == "remarks"))
async def ask_new_remarks(
    call: CallbackQuery, callback_data: lkb.EditCB, state: FSMContext
) -> None:
    await state.set_state(LogStates.editing_remarks)
    await state.update_data(flight_id=callback_data.flight_id, page=callback_data.page)
    await _render(
        call,
        "Пришлите заметку к рейсу. Чтобы очистить \u2014 отправьте <code>-</code>",
        lkb.edit_cancel_keyboard(callback_data.flight_id, callback_data.page),
    )
    await call.answer()


@router.message(StateFilter(LogStates.editing_remarks), F.text)
async def apply_new_remarks(
    message: Message, state: FSMContext, session: AsyncSession, pilot: Pilot,
    settings: Settings,
) -> None:
    data = await state.get_data()
    flight = await lrepo.get_flight(session, pilot.id, data["flight_id"])
    if flight is None:
        await message.answer("Запись потерялась. Откройте её заново: /logbook")
        await state.clear()
        return
    text = message.text.strip()
    await lrepo.edit_remarks(session, flight, None if text == "-" else text[:500])
    await session.commit()
    await state.clear()
    tz = pilot_tz(pilot, settings)
    await message.answer(
        f"\u2705 <b>Заметка сохранена</b>\n\n{_flight_card(flight, tz)}",
        reply_markup=lkb.flight_card_keyboard(flight.id, data.get("page", 0)),
    )


@router.callback_query(lkb.EditCB.filter(F.action == "history"))
async def show_history(
    call: CallbackQuery, callback_data: lkb.EditCB, session: AsyncSession, pilot: Pilot,
    settings: Settings,
) -> None:
    flight = await lrepo.get_flight(session, pilot.id, callback_data.flight_id)
    if flight is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    revisions = await lrepo.revisions_for(session, flight.id)
    tz = pilot_tz(pilot, settings)

    if not revisions:
        text = "\U0001f570\ufe0f <b>История правок</b>\n\nЗапись не изменялась."
    else:
        lines = ["\U0001f570\ufe0f <b>История правок</b>", ""]
        for revision in revisions:
            when = revision.changed_at.astimezone(tz)
            lines.append(
                f"{when:%d.%m.%Y %H:%M} \u2014 {esc(revision.field)}\n"
                f"  <s>{esc(revision.old_value or '\u2014')}</s> "
                f"\u2192 <b>{esc(revision.new_value or '\u2014')}</b>"
            )
        text = "\n".join(lines)

    await _render(call, text, lkb.flight_card_keyboard(flight.id, callback_data.page))
    await call.answer()
