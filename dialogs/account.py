from aiogram import F
from aiogram_dialog import Dialog, Window
from aiogram_dialog.widgets.kbd import Cancel, Button
from aiogram_dialog.widgets.text import Const, Format

from states.account import AccountState
from utils.getters import pilot_data_getter


def first_window():
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
        Button(text=Const('🛫 Show my flights'),
               id='my_flights_button',
               on_click=None),
        Button(text=Const('Delete account'),
               id='delete_account_button',
               on_click=None),
        Cancel(Const('◀️ Back'), id='exit'),
        getter=pilot_data_getter,
        state=AccountState.first_page
    )

account_dialog = Dialog(
    first_window(),
)