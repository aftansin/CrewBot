from aiogram.fsm.state import State, StatesGroup


class AccountState(StatesGroup):
    first_page = State()
    second_page = State()
