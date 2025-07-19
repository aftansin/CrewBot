from aiogram.fsm.state import State, StatesGroup


class AccountState(StatesGroup):
    account_info = State()
    events_window = State()
    event_info = State()
