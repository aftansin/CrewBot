"""Клавиатуры лётной книжки."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards import MenuCB
from app.db.models import Aircraft, Event


class LogCB(CallbackData, prefix="log"):
    action: str          # menu | pending | take | recent
    uid: str = ""


class TailCB(CallbackData, prefix="tail"):
    aircraft_id: int


def logbook_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u2708\ufe0f Записать рейс", callback_data=LogCB(action="pending"))
    builder.button(text="\U0001f4d6 Последние записи", callback_data=LogCB(action="recent"))
    builder.button(text="\u25c0\ufe0f Меню", callback_data=MenuCB(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def back_to_logbook(text: str = "\u25c0\ufe0f Книжка") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=LogCB(action="menu"))
    return builder.as_markup()


def pending_keyboard(events: list[Event], tz, limit: int = 10) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for event in events[:limit]:
        local = event.dtstart.astimezone(tz)
        route = f"{event.dep_code or '???'}\u2192{event.arr_code or '???'}"
        label = f"{local:%d.%m} {event.flight_no or ''} {route}"
        builder.row(
            InlineKeyboardButton(
                text=label[:64], callback_data=LogCB(action="take", uid=event.uid).pack()
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="\u25c0\ufe0f Книжка", callback_data=LogCB(action="menu").pack()
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
