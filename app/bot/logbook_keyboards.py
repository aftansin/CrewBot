"""Клавиатуры лётной книжки."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards import MenuCB
from app.db.models import Aircraft, Event

PAGE_SIZE = 8


class LogCB(CallbackData, prefix="log"):
    action: str          # menu | pending | take | recent
    uid: str = ""
    page: int = 0


class TailCB(CallbackData, prefix="tail"):
    aircraft_id: int


class FuncCB(CallbackData, prefix="func"):
    """Функция выбирается вручную, когда по ленте её определить не удалось."""
    value: str


def logbook_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u2708\ufe0f Записать рейс", callback_data=LogCB(action="pending"))
    builder.button(text="\U0001f4d6 Последние записи", callback_data=LogCB(action="recent"))
    builder.button(text="\U0001f50d Поиск", callback_data=FindCB(kind="menu"))
    builder.button(text="\U0001f4c4 Отчёты PDF", callback_data=ReportCB(kind="menu"))
    builder.button(text="\u2708\ufe0f Борты", callback_data=FleetCB(action="list"))
    builder.button(text="\u25c0\ufe0f Меню", callback_data=MenuCB(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def back_to_logbook(text: str = "\u25c0\ufe0f Книжка") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=LogCB(action="menu"))
    return builder.as_markup()


def _pager(builder, action: str, page: int, pages: int) -> None:
    if pages <= 1:
        return
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(
            text="\u25c0\ufe0f", callback_data=LogCB(action=action, page=page - 1).pack()))
    row.append(InlineKeyboardButton(
        text=f"{page + 1}/{pages}", callback_data=LogCB(action="noop").pack()))
    if page < pages - 1:
        row.append(InlineKeyboardButton(
            text="\u25b6\ufe0f", callback_data=LogCB(action=action, page=page + 1).pack()))
    builder.row(*row)


def pending_keyboard(events: list[Event], tz, page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    pages = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    for event in events[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        local = event.dtstart.astimezone(tz)
        route = f"{event.dep_code or '???'}\u2192{event.arr_code or '???'}"
        label = f"{local:%d.%m} {event.flight_no or ''} {route}"
        builder.row(
            InlineKeyboardButton(
                text=label[:64], callback_data=LogCB(action="take", uid=event.uid).pack()
            )
        )
    _pager(builder, "pending", page, pages)
    builder.row(
        InlineKeyboardButton(
            text="\u270d\ufe0f Записать вручную",
            callback_data=LogCB(action="manual").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def manual_entry_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u274c Отмена", callback_data=LogCB(action="menu"))
    return builder.as_markup()


def recent_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _pager(builder, "recent", page, pages)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def draft_keyboard(uid: str) -> InlineKeyboardMarkup:
    """Карточка рейса: ввод времён идёт сообщением, кнопки — для исключений."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="\U0001f6ab Рейс не выполнялся",
            callback_data=LogCB(action="skip", uid=uid).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def tail_keyboard(suggestions) -> InlineKeyboardMarkup:
    """Подсказки бортов. Причина выбора видна на кнопке — подстановка
    не должна выглядеть как непонятно откуда взявшийся номер."""
    builder = InlineKeyboardBuilder()
    for item in suggestions:
        mark = "\u2b50 " if item.confidence == "high" else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{item.display} \u2014 {item.reason}"[:64],
                callback_data=TailCB(aircraft_id=item.aircraft.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def tail_results_keyboard(found: list[Aircraft]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for aircraft in found:
        label = aircraft.display
        if aircraft.registration_ra and aircraft.registration != aircraft.registration_ra:
            label = f"{aircraft.registration_ra} ({aircraft.registration})"
        builder.row(
            InlineKeyboardButton(
                text=label[:64], callback_data=TailCB(aircraft_id=aircraft.id).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def after_save_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u2708\ufe0f Записать ещё", callback_data=LogCB(action="pending"))
    builder.button(text="\U0001f4d2 Книжка", callback_data=LogCB(action="menu"))
    builder.adjust(1)
    return builder.as_markup()


FUNCTION_CHOICES = (
    ("pic", "\U0001f9d1\u200d\u2708\ufe0f КВС"),
    ("copilot", "\U0001f464 Второй пилот"),
    ("cruise_relief", "\U0001f6cb\ufe0f Усиленный экипаж"),
    ("fi", "\U0001f393 Инструктор"),
)


def function_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in FUNCTION_CHOICES:
        builder.row(
            InlineKeyboardButton(text=label, callback_data=FuncCB(value=value).pack())
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


class EditCB(CallbackData, prefix="edit"):
    """Правка записи книжки."""
    action: str          # open | times | aircraft | function | remarks | history
    flight_id: int = 0
    page: int = 0


def flight_card_keyboard(flight_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="\U0001f552 Времена",
        callback_data=EditCB(action="times", flight_id=flight_id, page=page),
    )
    builder.button(
        text="\u2708\ufe0f Борт",
        callback_data=EditCB(action="aircraft", flight_id=flight_id, page=page),
    )
    builder.button(
        text="\U0001f9d1\u200d\u2708\ufe0f Функция",
        callback_data=EditCB(action="function", flight_id=flight_id, page=page),
    )
    builder.button(
        text="\U0001f4dd Заметка",
        callback_data=EditCB(action="remarks", flight_id=flight_id, page=page),
    )
    builder.adjust(2, 2)
    builder.row(
        InlineKeyboardButton(
            text="\U0001f570\ufe0f История правок",
            callback_data=EditCB(action="history", flight_id=flight_id, page=page).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f К записям",
            callback_data=LogCB(action="recent", page=page).pack(),
        )
    )
    return builder.as_markup()


def edit_cancel_keyboard(flight_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="\u274c Отмена",
        callback_data=EditCB(action="open", flight_id=flight_id, page=page),
    )
    return builder.as_markup()


def edit_function_keyboard(flight_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in FUNCTION_CHOICES:
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=FuncEditCB(value=value, flight_id=flight_id, page=page).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена",
            callback_data=EditCB(action="open", flight_id=flight_id, page=page).pack(),
        )
    )
    return builder.as_markup()


class FuncEditCB(CallbackData, prefix="fedit"):
    value: str
    flight_id: int
    page: int = 0


class TailEditCB(CallbackData, prefix="tedit"):
    aircraft_id: int
    flight_id: int
    page: int = 0


def edit_tail_keyboard(found, flight_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for aircraft in found:
        label = aircraft.display
        if aircraft.registration_ra and aircraft.registration != aircraft.registration_ra:
            label = f"{aircraft.registration_ra} ({aircraft.registration})"
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=TailEditCB(
                    aircraft_id=aircraft.id, flight_id=flight_id, page=page
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена",
            callback_data=EditCB(action="open", flight_id=flight_id, page=page).pack(),
        )
    )
    return builder.as_markup()


def recent_entry_keyboard(flights, page: int, pages: int) -> InlineKeyboardMarkup:
    """Записи кликабельны: нажатие открывает карточку рейса."""
    builder = InlineKeyboardBuilder()
    for flight in flights:
        # Номер рейса полезнее бортового: по нему узнаёшь рейс с ходу.
        # Борт виден в карточке, а если номера нет — он и подставится.
        marker = flight.flight_number or (
            flight.aircraft.display if flight.aircraft else ""
        )
        label = (
            f"{flight.flight_date:%d.%m.%y} "
            f"{flight.dep_icao}\u2192{flight.arr_icao} {marker}".rstrip()
        )
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=EditCB(
                    action="open", flight_id=flight.id, page=page
                ).pack(),
            )
        )
    _pager(builder, "recent", page, pages)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


class FindCB(CallbackData, prefix="find"):
    """Поиск по книжке."""
    kind: str = "menu"   # menu | crew | aircraft | airport


def search_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001f465 По экипажу", callback_data=FindCB(kind="crew"))
    builder.button(text="\u2708\ufe0f По борту", callback_data=FindCB(kind="aircraft"))
    builder.button(text="\U0001f5fa\ufe0f По аэропорту", callback_data=FindCB(kind="airport"))
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def search_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u25c0\ufe0f Другой поиск", callback_data=FindCB(kind="menu"))
    builder.button(text="\U0001f4d2 Книжка", callback_data=LogCB(action="menu"))
    builder.adjust(1)
    return builder.as_markup()


class ManualTailCB(CallbackData, prefix="mtail"):
    aircraft_id: int


def manual_tail_keyboard(found) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for aircraft in found:
        label = aircraft.display
        if aircraft.registration_ra and aircraft.registration != aircraft.registration_ra:
            label = f"{aircraft.registration_ra} ({aircraft.registration})"
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=ManualTailCB(aircraft_id=aircraft.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u274c Отмена", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


class ReportCB(CallbackData, prefix="rep"):
    """Отчёты в PDF."""
    kind: str = "menu"     # menu | summary | year | month | full
    year: int = 0
    month: int = 0


REPORT_TITLES = {
    "summary": "\U0001f4c4 Сводка по карьере",
    "year": "\U0001f4c5 За год",
    "month": "\U0001f5d3\ufe0f За месяц",
    "full": "\U0001f4da Вся книжка",
}


def report_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for kind in ("year", "month", "full", "summary"):
        builder.row(
            InlineKeyboardButton(
                text=REPORT_TITLES[kind], callback_data=ReportCB(kind=kind).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def report_years_keyboard(years: list[int], kind: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for year in sorted(years, reverse=True):
        builder.button(
            text=str(year), callback_data=ReportCB(kind=kind, year=year)
        )
    builder.adjust(4)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Отчёты", callback_data=ReportCB(kind="menu").pack()
        )
    )
    return builder.as_markup()


MONTH_NAMES = ("", "янв", "фев", "мар", "апр", "май", "июн",
               "июл", "авг", "сен", "окт", "ноя", "дек")


def report_months_keyboard(year: int, months: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for month in sorted(months):
        builder.button(
            text=MONTH_NAMES[month],
            callback_data=ReportCB(kind="month", year=year, month=month),
        )
    builder.adjust(4)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Годы", callback_data=ReportCB(kind="month").pack()
        )
    )
    return builder.as_markup()


class FleetCB(CallbackData, prefix="fleet"):
    """Борты книжки."""
    action: str = "list"     # list | card | add | type | ra | note
    aircraft_id: int = 0
    page: int = 0


def fleet_keyboard(items, page: int, pages: int) -> InlineKeyboardMarkup:
    """items — тройки (борт, рейсов, минут)."""
    builder = InlineKeyboardBuilder()
    for aircraft, _flights, minutes in items:
        used = f"{minutes // 60}ч" if minutes else "не летали"
        builder.row(
            InlineKeyboardButton(
                text=f"{aircraft.display}  {aircraft.type_code or ''}  {used}"[:64],
                callback_data=FleetCB(
                    action="card", aircraft_id=aircraft.id, page=page
                ).pack(),
            )
        )
    if pages > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton(
                text="\u25c0\ufe0f", callback_data=FleetCB(action="list", page=page - 1).pack()))
        row.append(InlineKeyboardButton(
            text=f"{page + 1}/{pages}", callback_data=LogCB(action="noop").pack()))
        if page < pages - 1:
            row.append(InlineKeyboardButton(
                text="\u25b6\ufe0f", callback_data=FleetCB(action="list", page=page + 1).pack()))
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(
            text="\u2795 Добавить борт", callback_data=FleetCB(action="add").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()


def aircraft_card_keyboard(aircraft_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="\U0001f6e0\ufe0f Тип",
        callback_data=FleetCB(action="type", aircraft_id=aircraft_id, page=page),
    )
    builder.button(
        text="\U0001f1f7\U0001f1fa Вторая регистрация",
        callback_data=FleetCB(action="ra", aircraft_id=aircraft_id, page=page),
    )
    builder.button(
        text="\U0001f4dd Заметка",
        callback_data=FleetCB(action="note", aircraft_id=aircraft_id, page=page),
    )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f К бортам",
            callback_data=FleetCB(action="list", page=page).pack(),
        )
    )
    return builder.as_markup()


def fleet_cancel_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u274c Отмена", callback_data=FleetCB(action="list", page=page))
    return builder.as_markup()



class BackupFormatCB(CallbackData, prefix="bkp"):
    """Формат резервной копии."""
    fmt: str = "json"


def backup_format_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001f5c3\ufe0f JSON (для восстановления)",
                   callback_data=BackupFormatCB(fmt="json"))
    builder.button(text="\U0001f4ca CSV (для Excel)",
                   callback_data=BackupFormatCB(fmt="csv"))
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
        )
    )
    return builder.as_markup()
