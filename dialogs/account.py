import operator
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, Window, DialogManager
from aiogram_dialog.widgets.kbd import Cancel, Button, ScrollingGroup, Select, Back
from aiogram_dialog.widgets.text import Const, Format

from db.db_requests import get_pilot_events
from states.account import AccountState
from utils.getters import pilot_data_getter, user_events_getter, event_info_getter
from utils.scheduler import check_pilot_calendar


def paginated_events(on_event_click):
    return ScrollingGroup(
        Select(
            Format('{item.short_summary}'),
            id='scroll_events',
            item_id_getter=operator.attrgetter('event_id'),
            items='events',
            on_click=on_event_click
        ),
        id='events_ids',
        width=1,
        height=5
    )


async def on_chosen_event(callback: CallbackQuery, widget: Select, dialog_manager: DialogManager, item_id: str):
    context = dialog_manager.current_context()
    context.dialog_data.update(event_id=item_id)
    await dialog_manager.switch_to(AccountState.event_info)


async def go_events_window(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    pilot_id = dialog_manager.middleware_data.get('db_pilot').id
    events = await get_pilot_events(dialog_manager.middleware_data.get('session'), pilot_id)
    # Сохраняем в dialog_data для использования в user_events_getter
    dialog_manager.dialog_data['events'] = events

    # Найдем страницу с ближайшим событием для отображения по умолчанию
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    closest_index = 0
    min_diff = float('inf')
    for i, event in enumerate(events):
        # Приводим dtstart к aware datetime, если он naive
        event_time = event.dtstart
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=ZoneInfo('Europe/Moscow'))

        diff = abs((event_time - now).total_seconds())
        if diff < min_diff:
            min_diff = diff
            closest_index = i
    page = closest_index // 5  # 5 - height из ScrollingGroup

    await dialog_manager.switch_to(state=AccountState.events_window)
    await dialog_manager.find('events_ids').set_page(page)


def account_info_window():
    return Window(
        Const(text=' 💳  <b>My Data:</b>'),
        Format("• <b>Username</b> ➜ {username}", when='username'),
        Format("• <b>Last Name</b> ➜ {last_name}", when='last_name'),
        Format("• <b>First Name</b> ➜ {first_name}", when='first_name'),
        Format("• <b>Middle Name</b> ➜ {middle_name}", when='middle_name'),
        Format("• <b>Registration date</b> ➜ {registration_date}"),
        Format("• <b>Subscription date</b> ➜ {subscription_date}", when='subscription_date'),
        Const("• <b>Calendar Link</b> ➜ ✅", when=F['ics_url']),
        Const("• <b>Calendar Link</b> ➜ ⛔️", when=~F['ics_url']),
        Button(text=Const('🛫 Show events'),
               id='my_flights_button',
               on_click=go_events_window),
        Cancel(Const('◀️ Back'), id='exit'),
        getter=pilot_data_getter,
        state=AccountState.account_info
    )

def events_window():
    return Window(
        Const("<b>Flight Time Data:</b>", when="has_flights"),
        Const("<b>❗️No data yet. Check your ics link.</b>", when=~F["has_flights"]),
        Format("• Last month: {previous_month_time}", when="has_flights"),
        Format("• <b>Current month:</b> {current_month_time}", when="has_flights"),
        Format("• Next month: {next_month_time}", when="has_flights"),
        Const("\n<i>To accurately calculate the flight time, it's necessary to adjust each flight.</i>",
              when=F["has_flights"]),
        paginated_events(on_chosen_event),
        Back(Const('◀️ Back')),
        state=AccountState.events_window,
        getter=user_events_getter
    )

def event_info():
    return Window(
        Format("<b>{short_summary}</b>"),
        Format("• ↗️ {dtstart}"),
        Format("• ↘️ {dtend}"),
        Format("\n<pre>{crew}</pre>"),
        Button(text=Const('Edit flight times'),
               id='edit_times_button',
               on_click=None,
               when='is_past'),
        Back(Const('◀️ Back')),
        state=AccountState.event_info,
        getter=event_info_getter
    )

account_dialog = Dialog(
    account_info_window(),
    events_window(),
    event_info()
)