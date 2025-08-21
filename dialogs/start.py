from aiogram import F
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, Dialog, Window
from aiogram_dialog.widgets.input import TextInput, ManagedTextInput
from aiogram_dialog.widgets.kbd import Button, Back, Cancel
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.text import Const, Format

from db.db_requests import update_ics_link
from states.account import AccountState
from states.start import StartState
from utils.getters import pilot_data_getter, qr_code_getter
from utils.scheduler import start_pilot_calendar_polling, remove_pilot_calendar_polling_job, check_pilot_calendar


async def go_ics_window(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    await dialog_manager.switch_to(state=StartState.ics_link)


async def go_donate_window(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    await dialog_manager.switch_to(state=StartState.donate)


# Проверка текста на то, что это ссылка
def url_check(url: str) -> str:
    prefix = "https://crew.aeroflot.ru/api/calendar/ics/"
    if url.startswith(prefix):
        return url
    raise ValueError


# Хэндлер, который сработает, если пользователь ввел корректный url
async def correct_url_handler(
        message: Message,
        widget: ManagedTextInput,
        dialog_manager: DialogManager,
        text: str) -> None:
    bot = dialog_manager.middleware_data.get('bot')
    pilot = dialog_manager.middleware_data.get('db_pilot')
    session = dialog_manager.middleware_data.get('session')
    scheduler = dialog_manager.middleware_data.get('scheduler')
    await message.answer(text=f'Success!')
    await dialog_manager.done()
    await update_ics_link(session, pilot.id, text)
    await check_pilot_calendar(bot, session, pilot)
    # await start_pilot_calendar_polling(bot, session, scheduler, pilot)



# Хэндлер, который сработает на ввод некорректного возраста
async def error_url_handler(
        message: Message,
        widget: ManagedTextInput,
        dialog_manager: DialogManager,
        error: ValueError):
    await message.answer(
        text='❗️ Incorrect URL. Please try again.'
    )


async def clear_ics_button(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    session = dialog_manager.middleware_data.get('session')
    scheduler = dialog_manager.middleware_data.get('scheduler')
    pilot = dialog_manager.middleware_data.get('db_pilot')
    await update_ics_link(session, pilot.id, None)
    await remove_pilot_calendar_polling_job(scheduler, pilot)


async def go_account_dialog_button(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    await dialog_manager.start(state=AccountState.account_info)


async def update_events(callback: CallbackQuery, button: Button, dialog_manager: DialogManager):
    bot = dialog_manager.middleware_data.get('bot')
    pilot = dialog_manager.middleware_data.get('db_pilot')
    session = dialog_manager.middleware_data.get('session')
    await dialog_manager.done()
    await check_pilot_calendar(bot, session, pilot)


def first_window():
    return Window(
        Const(text='🤖 <b>Main Menu.</b>'),
        Const(text='<i>To close dialog press exit or update button.</i>'),
        Button(text=Const('☕️  By me coffee'),
               id='donate',
               on_click=go_donate_window,
               when=~F["middleware_data"]["is_admin"]),
        Button(text=Const(' 💳  My data'),
               id='account_button',
               on_click=go_account_dialog_button),
        Button(text=Const('🔁 Update'),
               id='update_button',
               on_click=update_events,
               when='ics_url'),
        Button(text=Const('🌐 ics link'),
                   id='ics_button',
                   on_click=go_ics_window),
        Button(text=Const('🧑‍✈️  Pilots'),
               id='pilots_button',
               on_click=None,
               when=F["middleware_data"]["is_admin"]),
        Cancel(Const('Exit'), id='exit'),
        getter=pilot_data_getter,
        state=StartState.welcome
    )


def second_window():
    return Window(
        Format("⚠️ Current calendar URL is: \n<pre>{ics_url}</pre>\n", when=F['ics_url']),
        Format("⚠️ No current calendar URL.\n", when=~F['ics_url']),
        Const('❔ Enter new calendar URL bellow.'),
        TextInput(
            id='ics_input',
            type_factory=url_check,
            on_success=correct_url_handler,
            on_error=error_url_handler,
        ),
        Button(text=Const('Clear calendar link'),
               id='clear_calendar',
               on_click=clear_ics_button,
               when=F['ics_url']),
        Back(Const('◀️ Back'), id='back'),
        getter=pilot_data_getter,
        state=StartState.ics_link
    )


def donate_window():
    return Window(
        Const(text="💰 <b>If you'd like to make a donation, please scan the QR code above. "
                   "Your support helps me continue my work!</b>"),
        DynamicMedia('qr_code'),
        Cancel(Const('Exit'), id='exit'),
        getter=qr_code_getter,
        state=StartState.donate
    )


start_dialog = Dialog(
    first_window(),
    second_window(),
    donate_window()
)
