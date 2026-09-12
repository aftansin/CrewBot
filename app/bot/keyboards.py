"""Клавиатуры и callback-данные.

Вместо aiogram-dialog — обычные inline-клавиатуры. Это снимает проблему
хранения ORM-объектов в FSM-storage (со старым кодом переезд на Redis
сломал бы бота на сериализации) и делает пагинацию явной.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Event
from app.sync.render import KIND_ICON, fmt_time, short_title

PAGE_SIZE = 6


class MenuCB(CallbackData, prefix="menu"):
    action: str  # main | refresh | plan | stats | settings | link | unlink | help


class PlanCB(CallbackData, prefix="plan"):
    page: int
    scope: str  # upcoming | month


class EventCB(CallbackData, prefix="ev"):
    event_id: int
    page: int
    scope: str


class IntervalCB(CallbackData, prefix="ival"):
    minutes: int


class AdminCB(CallbackData, prefix="adm"):
    action: str  # list | pilot | sync
    pilot_id: int = 0
    page: int = 0


class ConfirmCB(CallbackData, prefix="cfm"):
    action: str
    yes: bool


def main_menu(has_link: bool, is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_link:
        builder.button(text="\U0001f501 Обновить план", callback_data=MenuCB(action="refresh"))
        builder.button(text="\U0001f4c5 Мой план", callback_data=PlanCB(page=0, scope="upcoming"))
        builder.button(text="\U0001f4ca Налёт", callback_data=MenuCB(action="stats"))
        builder.button(text="\u2699\ufe0f Настройки", callback_data=MenuCB(action="settings"))
        builder.adjust(1, 2, 1)
    else:
        builder.button(text="\U0001f517 Привязать календарь", callback_data=MenuCB(action="link"))
        builder.button(text="\u2139\ufe0f Помощь", callback_data=MenuCB(action="help"))
        builder.adjust(1)

    if is_admin:
        builder.row(
            InlineKeyboardButton(
                text="\U0001f9d1\u200d\u2708\ufe0f Пользователи",
                callback_data=AdminCB(action="list").pack(),
            )
        )
    return builder.as_markup()


def back_to_menu(text: str = "\u25c0\ufe0f Меню") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=MenuCB(action="main"))
    return builder.as_markup()


def plan_keyboard(
    events: list[Event],
    page: int,
    total_pages: int,
    scope: str,
    tz,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for event in events:
        icon = KIND_ICON.get(event.kind, "\U0001f4c5")
        local = event.dtstart.astimezone(tz)
        label = f"{icon} {local:%d.%m} {fmt_time(event.dtstart, tz)} {short_title(event)}"
        builder.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=EventCB(event_id=event.id, page=page, scope=scope).pack(),
            )
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="\u25c0\ufe0f", callback_data=PlanCB(page=page - 1, scope=scope).pack()
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data=MenuCB(action="noop").pack(),
            )
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="\u25b6\ufe0f", callback_data=PlanCB(page=page + 1, scope=scope).pack()
                )
            )
        builder.row(*nav)

    other_scope = "month" if scope == "upcoming" else "upcoming"
    other_label = "\U0001f5d3 Текущий месяц" if scope == "upcoming" else "\u23ed Только предстоящее"
    builder.row(
        InlineKeyboardButton(
            text=other_label, callback_data=PlanCB(page=0, scope=other_scope).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(text="\u25c0\ufe0f Меню", callback_data=MenuCB(action="main").pack())
    )
    return builder.as_markup()


def event_keyboard(page: int, scope: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u25c0\ufe0f К плану", callback_data=PlanCB(page=page, scope=scope))
    builder.button(text="\U0001f3e0 Меню", callback_data=MenuCB(action="main"))
    builder.adjust(2)
    return builder.as_markup()


INTERVAL_CHOICES = (30, 60, 120, 180, 360, 720)


def settings_keyboard(current_interval: int, notifications_on: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for minutes in INTERVAL_CHOICES:
        mark = "\u2705 " if minutes == current_interval else ""
        label = f"{mark}{minutes} мин" if minutes < 60 else f"{mark}{minutes // 60} ч"
        builder.button(text=label, callback_data=IntervalCB(minutes=minutes))
    builder.adjust(3, 3)

    bell = "\U0001f514 Уведомления: вкл" if notifications_on else "\U0001f515 Уведомления: выкл"
    builder.row(
        InlineKeyboardButton(text=bell, callback_data=MenuCB(action="toggle_notify").pack())
    )
    builder.row(
        InlineKeyboardButton(
            text="\U0001f517 Сменить ссылку", callback_data=MenuCB(action="link").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="\U0001f5d1 Отвязать календарь", callback_data=MenuCB(action="unlink").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(text="\u25c0\ufe0f Меню", callback_data=MenuCB(action="main").pack())
    )
    return builder.as_markup()


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\u2705 Да", callback_data=ConfirmCB(action=action, yes=True))
    builder.button(text="\u274c Нет", callback_data=ConfirmCB(action=action, yes=False))
    builder.adjust(2)
    return builder.as_markup()


def admin_list_keyboard(pilots, page: int, total_pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pilot in pilots:
        mark = "\U0001f7e2" if pilot.ics_url else "\u26aa"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {pilot.display_name}"[:64],
                callback_data=AdminCB(action="pilot", pilot_id=pilot.id, page=page).pack(),
            )
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="\u25c0\ufe0f", callback_data=AdminCB(action="list", page=page - 1).pack()
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}", callback_data=MenuCB(action="noop").pack()
            )
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="\u25b6\ufe0f", callback_data=AdminCB(action="list", page=page + 1).pack()
                )
            )
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(text="\u25c0\ufe0f Меню", callback_data=MenuCB(action="main").pack())
    )
    return builder.as_markup()


def admin_pilot_keyboard(pilot_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="\U0001f501 Синхронизировать",
        callback_data=AdminCB(action="sync", pilot_id=pilot_id, page=page),
    )
    builder.button(text="\u25c0\ufe0f К списку", callback_data=AdminCB(action="list", page=page))
    builder.adjust(1)
    return builder.as_markup()
