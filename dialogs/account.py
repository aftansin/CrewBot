import operator

from aiogram import F
from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, Window, DialogManager
from aiogram_dialog.widgets.kbd import Cancel, Button, ScrollingGroup, Select, Back
from aiogram_dialog.widgets.text import Const, Format

from states.account import AccountState
from utils.getters import pilot_data_getter, user_events_getter


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
    await dialog_manager.switch_to(state=AccountState.events_window)


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
        Button(text=Const('Delete account'),
               id='delete_account_button',
               on_click=None),
        Cancel(Const('◀️ Back'), id='exit'),
        getter=pilot_data_getter,
        state=AccountState.account_info
    )

def events_window():
    return Window(
        Const("<b>Наряд:</b>"),
        paginated_events(on_chosen_event),
        Back(Const('Back')),
        state=AccountState.events_window,
        getter=user_events_getter
    )

account_dialog = Dialog(
    account_info_window(),
    events_window()
)