"""Экраны лётной книжки.

Поток записи рейса устроен вокруг одного наблюдения: после рейса пилот
готов напечатать ровно одну строку, а не проходить мастер из шести шагов.
Поэтому карточка показывает всё, что известно из расписания, и просит
только времена. Борт подставляется, экипаж берётся из ленты, функция
выводится из должности в задании. Всё это правится потом, если нужно.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import logbook_keyboards as lkb
from app.bot.handlers.user import _render, pilot_tz
from app.config import Settings
from app.db import logbook_repo as lrepo
from app.db.models import FlightSource, Function, Pilot
from app.icalendar_feed.parse import month_bounds
from app.logbook.backup import (
    build_backup,
    build_csv,
    csv_filename,
    filename_for,
    summary_text,
    to_bytes,
)
from app.logbook.draft import draft_from_event, resolve_airports, suggested_function
from app.logbook.tail import suggest_tail
from app.logbook.timeinput import (
    Severity,
    build_datetimes,
    parse_and_validate,
    parse_manual_flight,
    validate,
)
from app.reports import pdf as pdf_reports
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
    searching = State()
    manual_entry = State()
    manual_tail = State()
    fleet_add = State()
    fleet_type = State()
    fleet_ra = State()
    fleet_note = State()


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
            "\u2705 Все рейсы из расписания записаны.\n\n"
            "<i>Летали где-то ещё? Можно завести рейс вручную.</i>",
            lkb.pending_keyboard([], tz, 0),
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

    # Список рейсов уже на кнопках — повторять его в тексте незачем.
    # Вместо этого показываем то, чего на кнопках нет: итог по странице.
    page_block = sum(f.block_minutes for f in flights)
    page_night = sum(f.night_minutes for f in flights)
    oldest, newest = flights[-1].flight_date, flights[0].flight_date

    lines = [
        f"\U0001f4d6 <b>Записи</b>  \u2014 всего {total}",
        "",
        f"Страница {page + 1} из {pages}: {oldest:%d.%m.%y} \u2013 {newest:%d.%m.%y}",
        f"Налёт на странице: <b>{fmt_minutes(page_block)}</b>"
        + (f"   ночь {fmt_minutes(page_night)}" if page_night else ""),
        "",
        "<i>Нажмите на запись, чтобы посмотреть или поправить.</i>",
    ]
    await _render(call, "\n".join(lines), lkb.recent_entry_keyboard(flights, page, pages))
    await call.answer()



# --------------------------------------------------------------------------
# Правка сохранённой записи
# --------------------------------------------------------------------------


def _night_note(flight) -> str:
    """Откуда взято ночное время.

    "не рассчитано" на перенесённой записи вводит в заблуждение: там оно
    посчитано LogTen и верно. Формулой пересчитываются только новые
    рейсы — на годах до 2022 старые цифры расходятся с ней до получаса,
    и переписывать их было бы подгонкой.
    """
    if flight.night_computed:
        return ""
    if flight.source is FlightSource.LOGTEN:
        return "  <i>(из LogTen)</i>"
    return "  <i>(нет координат аэропорта)</i>"


def _flight_card(flight, tz, crew=None) -> str:
    tail = flight.aircraft.display if flight.aircraft else "\u2014"
    times = (
        f"{flight.out_utc:%H:%M} \u2013 {flight.in_utc:%H:%M} UTC"
        if flight.out_utc and flight.in_utc
        else ""
    )

    lines = [
        f"\u2708\ufe0f <b>{esc(flight.flight_number or 'Рейс')}</b>   "
        f"{flight.dep_icao} \u2192 {flight.arr_icao}",
        f"<b>{flight.flight_date:%d.%m.%Y}</b>" + (f"   {times}" if times else ""),
        "",
        f"Блок-тайм   <b>{fmt_minutes(flight.block_minutes)}</b>",
    ]
    if flight.night_minutes:
        lines.append(
            f"Ночь        {fmt_minutes(flight.night_minutes)}{_night_note(flight)}"
        )
    lines.append(f"Борт        <b>{esc(tail)}</b>")
    lines.append(f"Функция     <b>{esc(function_label(flight.function))}</b>")
    if flight.duty_minutes:
        lines.append(f"Смена       {fmt_minutes(flight.duty_minutes)}")

    if crew:
        lines.append("")
        lines.append("<b>Экипаж</b>")
        for person, role in crew:
            lines.append(f"\u2022 {esc(person)} \u2014 {esc(role)}")

    if flight.remarks:
        lines.append("")
        lines.append(f"\U0001f4dd <i>{esc(flight.remarks)}</i>")

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
    crew = await lrepo.crew_of(session, flight.id)
    await _render(
        call,
        _flight_card(flight, tz, crew),
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



# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------

SEARCH_PROMPTS = {
    "crew": (
        "\U0001f465 <b>Поиск по экипажу</b>\n\n"
        "Пришлите фамилию. Можно на любом языке и в любом написании \u2014 "
        "<code>Цибульников</code>, <code>Tsibulnikov</code> и "
        "<code>Tsybulnikov</code> найдут одного человека."
    ),
    "aircraft": (
        "\u2708\ufe0f <b>Поиск по борту</b>\n\n"
        "Пришлите регистрацию или её часть: <code>73125</code> или <code>BCD</code>."
    ),
    "airport": (
        "\U0001f5fa\ufe0f <b>Поиск по аэропорту</b>\n\n"
        "Пришлите код ICAO или IATA: <code>URSS</code> или <code>AER</code>."
    ),
}

SEARCHERS = {
    "crew": lrepo.search_by_crew,
    "aircraft": lrepo.search_by_aircraft,
    "airport": lrepo.search_by_airport,
}


@router.callback_query(lkb.FindCB.filter(F.kind == "menu"))
async def search_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render(
        call,
        "\U0001f50d <b>Поиск по книжке</b>\n\nЧто ищем?",
        lkb.search_menu(),
    )
    await call.answer()


@router.callback_query(lkb.FindCB.filter(F.kind != "menu"))
async def ask_search_query(
    call: CallbackQuery, callback_data: lkb.FindCB, state: FSMContext
) -> None:
    await state.set_state(LogStates.searching)
    await state.update_data(kind=callback_data.kind)
    await _render(call, SEARCH_PROMPTS[callback_data.kind], lkb.search_cancel_keyboard())
    await call.answer()


@router.message(StateFilter(LogStates.searching), F.text)
async def run_search(
    message: Message, state: FSMContext, session: AsyncSession, pilot: Pilot
) -> None:
    data = await state.get_data()
    kind = data.get("kind", "crew")
    result = await SEARCHERS[kind](session, pilot.id, message.text.strip())

    if result is None or not result.total:
        await message.answer(
            "Ничего не нашлось. Попробуйте другое написание или код.",
            reply_markup=lkb.search_cancel_keyboard(),
        )
        return

    lines = [
        f"\U0001f50d <b>{esc(result.subject)}</b>",
        "",
        f"Рейсов: <b>{result.total}</b>   налёт: <b>{fmt_minutes(result.block)}</b>",
    ]
    if result.night:
        lines.append(f"Ночь: {fmt_minutes(result.night)}")
    if result.pic or result.copilot:
        lines.append(
            f"КВС {fmt_minutes(result.pic)} \u00b7 второй пилот {fmt_minutes(result.copilot)}"
        )

    lines.append("")
    lines.append("<b>Последние рейсы</b>")
    for flight in result.flights:
        marker = flight.flight_number or (
            flight.aircraft.display if flight.aircraft else ""
        )
        lines.append(
            f"{flight.flight_date:%d.%m.%y} "
            f"{flight.dep_icao}\u2192{flight.arr_icao} "
            f"{fmt_minutes(flight.block_minutes)}  {esc(marker)}"
        )
    if result.total > len(result.flights):
        lines.append(f"<i>\u2026 и ещё {result.total - len(result.flights)}</i>")

    if kind != "crew" and result.partners:
        lines.append("")
        lines.append("<b>Чаще всего летали с</b>")
        for name, count in sorted(result.partners.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  {count:4}  {esc(name)}")

    await state.clear()
    await message.answer("\n".join(lines), reply_markup=lkb.search_cancel_keyboard())



# --------------------------------------------------------------------------
# Резервная копия
# --------------------------------------------------------------------------


@router.message(Command("backup"))
async def cmd_backup(message: Message, session: AsyncSession, pilot: Pilot) -> None:
    await _send_backup(message, session, pilot.id)


@router.callback_query(lkb.LogCB.filter(F.action == "backup"))
async def choose_backup_format(call: CallbackQuery) -> None:
    await _render(
        call,
        "\U0001f4be <b>Резервная копия</b>\n\n"
        "<b>JSON</b> \u2014 полная копия: рейсы, экипажи, справочники, "
        "история правок. Из неё книжку можно восстановить.\n\n"
        "<b>CSV</b> \u2014 таблица для Excel: одна строка на рейс. "
        "Удобно смотреть и переносить в другие программы, но это не "
        "копия для восстановления.",
        lkb.backup_format_keyboard(),
    )
    await call.answer()


@router.callback_query(lkb.BackupFormatCB.filter())
async def make_backup(
    call: CallbackQuery, callback_data: lkb.BackupFormatCB,
    session: AsyncSession, pilot: Pilot,
) -> None:
    await call.answer("Собираю копию\u2026")
    if call.message is None:
        return
    if callback_data.fmt == "csv":
        await _send_csv(call.message, session, pilot.id)
    else:
        await _send_backup(call.message, session, pilot.id)


async def _send_csv(message: Message, session: AsyncSession, pilot_id: int) -> None:
    data = await build_csv(session, pilot_id)
    rows = data.decode("utf-8-sig").count("\r\n") - 1
    document = BufferedInputFile(data, filename=csv_filename(pilot_id))
    await message.answer_document(
        document,
        caption=(
            "\U0001f4ca <b>Книжка в CSV</b>\n\n"
            f"Рейсов: {rows}\n\n"
            "<i>Разделитель \u2014 точка с запятой, кодировка UTF-8 с BOM: "
            "Excel откроет как надо.</i>"
        ),
        reply_markup=lkb.back_to_logbook(),
    )


async def _send_backup(message: Message, session: AsyncSession, pilot_id: int) -> None:
    data = await build_backup(session, pilot_id)
    document = BufferedInputFile(to_bytes(data), filename=filename_for(pilot_id))
    await message.answer_document(
        document,
        caption=(
            "\U0001f4be <b>Резервная копия книжки</b>\n\n"
            f"{summary_text(data)}\n\n"
            "<i>Сохраните файл вне Telegram \u2014 копия в том же месте, "
            "что и оригинал, копией не является.</i>"
        ),
        reply_markup=lkb.back_to_logbook(),
    )



# --------------------------------------------------------------------------
# Ручной ввод рейса
# --------------------------------------------------------------------------


@router.callback_query(lkb.LogCB.filter(F.action == "manual"))
async def ask_manual_flight(call: CallbackQuery, state: FSMContext) -> None:
    """Рейс, которого нет в расписании: перегонка, полёт в другом месте."""
    await state.set_state(LogStates.manual_entry)
    await _render(
        call,
        "\u270d\ufe0f <b>Рейс вручную</b>\n\n"
        "Пришлите одной строкой пять значений \u2014 дату, откуда, куда "
        "и времена запуска и выключения <u>в UTC</u>:\n\n"
        "<code>14.09.2026 UUEE UIII 2238 0401</code>\n\n"
        "<i>Коды аэропортов в ICAO, из четырёх букв.</i>",
        lkb.manual_entry_keyboard(),
    )
    await call.answer()


@router.message(StateFilter(LogStates.manual_entry), F.text)
async def receive_manual_flight(
    message: Message, state: FSMContext, session: AsyncSession, pilot: Pilot,
    settings: Settings,
) -> None:
    tz = pilot_tz(pilot, settings)
    parsed = parse_manual_flight(message.text, datetime.now(tz=tz).date())

    if not parsed.ok:
        text = "\n".join(
            f"\u274c {esc(i.message)}" + (f"\n   <i>{esc(i.hint)}</i>" if i.hint else "")
            for i in parsed.issues
        )
        await message.answer(f"{text}\n\nПопробуйте ещё раз.")
        return

    out_dt, in_dt = build_datetimes(
        datetime.combine(parsed.flight_date, datetime.min.time(), tzinfo=UTC),
        parsed.out,
        parsed.inn,
    )
    issues = validate(out_dt, in_dt)
    if any(i.severity == Severity.ERROR for i in issues):
        text = "\n".join(f"\u274c {esc(i.message)}" for i in issues)
        await message.answer(f"{text}\n\nПопробуйте ещё раз.")
        return

    # Неизвестный аэропорт не запрещаем: книжка не обязана знать все
    # площадки мира. Но предупреждаем — по нему не посчитается ночное.
    warnings = [f"\u26a0\ufe0f {esc(i.message)}" for i in issues]
    for code in (parsed.dep, parsed.arr):
        if await lrepo.known_airport(session, code) is None:
            warnings.append(
                f"\u26a0\ufe0f Аэропорт {code} не в справочнике \u2014 "
                "ночное время по нему не посчитается."
            )

    await state.set_state(LogStates.manual_tail)
    await state.update_data(
        manual={
            "date": parsed.flight_date.isoformat(),
            "dep": parsed.dep,
            "arr": parsed.arr,
            "out": out_dt.isoformat(),
            "inn": in_dt.isoformat(),
        }
    )

    block = int((in_dt - out_dt).total_seconds() // 60)
    lines = [
        f"\u2708\ufe0f {parsed.dep} \u2192 {parsed.arr}  "
        f"{parsed.flight_date:%d.%m.%Y}",
        f"Блок-тайм: <b>{fmt_minutes(block)}</b>",
    ]
    lines.extend(warnings)
    lines += ["", "<b>Какой борт?</b>", "Пришлите регистрацию или её часть."]
    await message.answer("\n".join(lines), reply_markup=lkb.manual_entry_keyboard())


@router.message(StateFilter(LogStates.manual_tail), F.text)
async def search_manual_tail(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    found = await lrepo.search_aircraft(session, message.text.strip())
    if not found:
        await message.answer(
            "Ничего не нашлось.\n\n"
            "<i>Если этого борта в книжке ещё нет, заведите его: "
            "меню книжки \u2192 Борты \u2192 Добавить.</i>"
        )
        return
    await message.answer(
        f"Найдено: {len(found)}", reply_markup=lkb.manual_tail_keyboard(found)
    )


@router.callback_query(lkb.ManualTailCB.filter())
async def save_manual_flight(
    call: CallbackQuery, callback_data: lkb.ManualTailCB, session: AsyncSession,
    pilot: Pilot, settings: Settings, state: FSMContext,
) -> None:
    data = (await state.get_data()).get("manual")
    if not data:
        await call.answer("Сессия истекла, начните заново", show_alert=True)
        return

    flight = await lrepo.create_manual_flight(
        session,
        pilot.id,
        flight_date=date.fromisoformat(data["date"]),
        dep_icao=data["dep"],
        arr_icao=data["arr"],
        out_utc=datetime.fromisoformat(data["out"]),
        in_utc=datetime.fromisoformat(data["inn"]),
        aircraft_id=callback_data.aircraft_id,
    )
    await session.commit()
    await state.clear()

    tz = pilot_tz(pilot, settings)
    await _render(
        call,
        "\u2705 <b>Записано вручную</b>\n\n"
        + _flight_card(flight, tz)
        + "\n\n<i>Функцию и заметку можно задать кнопками ниже.</i>",
        lkb.flight_card_keyboard(flight.id, 0),
    )
    await call.answer("Сохранено")



# --------------------------------------------------------------------------
# Отчёты в PDF
# --------------------------------------------------------------------------

MONTHS_RU = ("", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")

REPORT_HINTS = {
    "summary": "Итоги по годам и типам ВС на одной странице. Для резюме.",
    "year": "Месяцы таблицей плюс итог за год.",
    "month": "Каждый рейс строкой за один месяц.",
    "full": "Вся книжка, каждый рейс строкой. Файл большой.",
}


def _owner_name(pilot: Pilot) -> str:
    return pilot.display_name


@router.callback_query(lkb.ReportCB.filter(F.kind == "menu"))
async def report_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    lines = ["\U0001f4c4 <b>Отчёты PDF</b>", ""]
    for kind in ("year", "month", "full", "summary"):
        lines.append(f"{lkb.REPORT_TITLES[kind]} \u2014 <i>{REPORT_HINTS[kind]}</i>")
    await _render(call, "\n".join(lines), lkb.report_menu())
    await call.answer()


@router.callback_query(lkb.ReportCB.filter(F.kind.in_({"summary", "full"})))
async def whole_logbook_report(
    call: CallbackQuery, callback_data: lkb.ReportCB, session: AsyncSession, pilot: Pilot
) -> None:
    await call.answer("Собираю отчёт\u2026")
    flights = await lrepo.flights_for_report(session, pilot.id)
    if not flights:
        await _render(call, "В книжке пока нет записей.", lkb.back_to_logbook())
        return

    owner = _owner_name(pilot)
    if callback_data.kind == "summary":
        data = pdf_reports.build_summary(owner, flights)
        caption = "\U0001f4c4 <b>Сводка по карьере</b>"
    else:
        data = pdf_reports.build_full(owner, flights)
        caption = "\U0001f4da <b>Вся книжка</b>"

    await _send_pdf(call.message, data, callback_data.kind, None, caption, len(flights))


@router.callback_query(lkb.ReportCB.filter(F.kind == "year"))
async def year_report(
    call: CallbackQuery, callback_data: lkb.ReportCB, session: AsyncSession, pilot: Pilot
) -> None:
    if not callback_data.year:
        years = await lrepo.logged_years(session, pilot.id)
        if not years:
            await _render(call, "В книжке пока нет записей.", lkb.back_to_logbook())
            await call.answer()
            return
        await _render(
            call, "За какой год?", lkb.report_years_keyboard(years, "year")
        )
        await call.answer()
        return

    await call.answer("Собираю отчёт\u2026")
    flights = await lrepo.flights_for_report(session, pilot.id, callback_data.year)
    data = pdf_reports.build_year(_owner_name(pilot), flights, callback_data.year)
    await _send_pdf(
        call.message, data, "year", str(callback_data.year),
        f"\U0001f4c5 <b>Отчёт за {callback_data.year} год</b>", len(flights),
    )


@router.callback_query(lkb.ReportCB.filter(F.kind == "month"))
async def month_report(
    call: CallbackQuery, callback_data: lkb.ReportCB, session: AsyncSession, pilot: Pilot
) -> None:
    if not callback_data.year:
        years = await lrepo.logged_years(session, pilot.id)
        if not years:
            await _render(call, "В книжке пока нет записей.", lkb.back_to_logbook())
            await call.answer()
            return
        await _render(call, "За какой год?", lkb.report_years_keyboard(years, "month"))
        await call.answer()
        return

    if not callback_data.month:
        months = await lrepo.logged_months(session, pilot.id, callback_data.year)
        await _render(
            call,
            f"За какой месяц {callback_data.year} года?",
            lkb.report_months_keyboard(callback_data.year, months),
        )
        await call.answer()
        return

    await call.answer("Собираю отчёт\u2026")
    flights = await lrepo.flights_for_report(
        session, pilot.id, callback_data.year, callback_data.month
    )
    data = pdf_reports.build_month(
        _owner_name(pilot), flights, callback_data.year, callback_data.month
    )
    period = f"{callback_data.year}-{callback_data.month:02d}"
    await _send_pdf(
        call.message, data, "month", period,
        f"\U0001f5d3\ufe0f <b>{MONTHS_RU[callback_data.month]} "
        f"{callback_data.year}</b>", len(flights),
    )


async def _send_pdf(
    message: Message, data: bytes, kind: str, period: str | None,
    caption: str, count: int,
) -> None:
    document = BufferedInputFile(data, filename=pdf_reports.filename(kind, period))
    await message.answer_document(
        document,
        caption=f"{caption}\n\nРейсов в отчёте: {count}",
        reply_markup=lkb.report_menu(),
    )



# --------------------------------------------------------------------------
# Борты
# --------------------------------------------------------------------------

FLEET_PAGE = 10


@router.callback_query(lkb.FleetCB.filter(F.action == "list"))
async def fleet_list(
    call: CallbackQuery, callback_data: lkb.FleetCB, session: AsyncSession,
    pilot: Pilot, state: FSMContext,
) -> None:
    await state.clear()
    items = await lrepo.list_aircraft_with_use(session, pilot.id)
    if not items:
        await _render(call, "Бортов пока нет.", lkb.fleet_keyboard([], 0, 1))
        await call.answer()
        return

    pages = max(1, (len(items) + FLEET_PAGE - 1) // FLEET_PAGE)
    page = max(0, min(callback_data.page, pages - 1))
    chunk = items[page * FLEET_PAGE : (page + 1) * FLEET_PAGE]

    flown = sum(1 for _a, count, _m in items if count)
    total = sum(minutes for _a, _c, minutes in items)
    text = (
        f"\u2708\ufe0f <b>Борты</b> \u2014 всего {len(items)}\n"
        f"Летали на {flown}, суммарно {fmt_minutes(total)}\n\n"
        "<i>Нажмите на борт, чтобы посмотреть или поправить.</i>"
    )
    await _render(call, text, lkb.fleet_keyboard(chunk, page, pages))
    await call.answer()


@router.callback_query(lkb.FleetCB.filter(F.action == "card"))
async def fleet_card(
    call: CallbackQuery, callback_data: lkb.FleetCB, session: AsyncSession, pilot: Pilot
) -> None:
    aircraft = await lrepo.aircraft_by_id(session, callback_data.aircraft_id)
    if aircraft is None:
        await call.answer("Борт не найден", show_alert=True)
        return

    items = await lrepo.list_aircraft_with_use(session, pilot.id)
    stats = next(((c, m) for a, c, m in items if a.id == aircraft.id), (0, 0))

    lines = [f"\u2708\ufe0f <b>{esc(aircraft.display)}</b>", ""]
    if aircraft.registration_ra and aircraft.registration_ra != aircraft.registration:
        lines.append(f"Регистрации  {esc(aircraft.registration)} / "
                     f"{esc(aircraft.registration_ra)}")
    else:
        lines.append(f"Регистрация  {esc(aircraft.registration)}")
    lines.append(f"Тип          {esc(aircraft.type_name or '\u2014')}"
                 + (f"  ({esc(aircraft.type_code)})" if aircraft.type_code else ""))
    lines.append(f"Рейсов       {stats[0]}")
    lines.append(f"Налёт        <b>{fmt_minutes(stats[1])}</b>")
    if aircraft.note:
        lines += ["", f"\U0001f4dd <i>{esc(aircraft.note)}</i>"]

    await _render(
        call, "\n".join(lines),
        lkb.aircraft_card_keyboard(aircraft.id, callback_data.page),
    )
    await call.answer()


@router.callback_query(lkb.FleetCB.filter(F.action == "add"))
async def fleet_ask_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LogStates.fleet_add)
    await _render(
        call,
        "\u2795 <b>Новый борт</b>\n\n"
        "Регистрация и тип одной строкой:\n"
        "<code>RA-73126/VQ-BWF 737-800 (B738)</code>\n\n"
        "Через слэш \u2014 вторая регистрация, если у борта их две. "
        "Тип можно не указывать:\n"
        "<code>N172SP Cessna 172</code>\n"
        "<code>RA-73126</code>",
        lkb.fleet_cancel_keyboard(),
    )
    await call.answer()


@router.message(StateFilter(LogStates.fleet_add), F.text)
async def fleet_create(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    parsed = lrepo.parse_aircraft_line(message.text)
    if parsed is None:
        await message.answer(
            "\u274c Не разобрал. Первым должна идти регистрация.\n"
            "Например: <code>RA-73126 737-800</code>"
        )
        return

    registration, registration_ra, type_name = parsed
    aircraft = await lrepo.create_aircraft(
        session, registration, registration_ra, type_name
    )
    if aircraft is None:
        await message.answer(
            f"Борт {esc(registration)} уже есть в книжке.",
            reply_markup=lkb.fleet_cancel_keyboard(),
        )
        return

    await session.commit()
    await state.clear()
    await message.answer(
        f"\u2705 Борт <b>{esc(aircraft.display)}</b> добавлен."
        + (f"\nТип: {esc(aircraft.type_name)}" if aircraft.type_name else ""),
        reply_markup=lkb.aircraft_card_keyboard(aircraft.id, 0),
    )


FLEET_PROMPTS = {
    "type": ("тип борта", "<code>737-800 (B738)</code>", LogStates.fleet_type),
    "ra": ("вторую регистрацию", "<code>RA-73126</code>", LogStates.fleet_ra),
    "note": ("заметку", "<code>SELCAL AB-CD</code>", LogStates.fleet_note),
}


@router.callback_query(lkb.FleetCB.filter(F.action.in_({"type", "ra", "note"})))
async def fleet_ask_field(
    call: CallbackQuery, callback_data: lkb.FleetCB, state: FSMContext
) -> None:
    label, example, target = FLEET_PROMPTS[callback_data.action]
    await state.set_state(target)
    await state.update_data(aircraft_id=callback_data.aircraft_id, page=callback_data.page)
    await _render(
        call,
        f"Пришлите {label}:\n{example}\n\n"
        "<i>Чтобы очистить \u2014 отправьте <code>-</code></i>",
        lkb.fleet_cancel_keyboard(callback_data.page),
    )
    await call.answer()


@router.message(
    StateFilter(LogStates.fleet_type, LogStates.fleet_ra, LogStates.fleet_note), F.text
)
async def fleet_apply_field(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    current = await state.get_state()
    data = await state.get_data()
    aircraft = await lrepo.aircraft_by_id(session, data["aircraft_id"])
    if aircraft is None:
        await message.answer("Борт потерялся. Откройте заново: /logbook")
        await state.clear()
        return

    value = message.text.strip()
    value = "" if value == "-" else value
    if current == LogStates.fleet_type.state:
        await lrepo.update_aircraft(session, aircraft, type_name=value)
    elif current == LogStates.fleet_ra.state:
        await lrepo.update_aircraft(session, aircraft, registration_ra=value)
    else:
        await lrepo.update_aircraft(session, aircraft, note=value[:300])

    await session.commit()
    await state.clear()
    await message.answer(
        f"\u2705 Обновлено: <b>{esc(aircraft.display)}</b>",
        reply_markup=lkb.aircraft_card_keyboard(aircraft.id, data.get("page", 0)),
    )
