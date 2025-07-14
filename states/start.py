from aiogram.fsm.state import State, StatesGroup


class StartState(StatesGroup):
    welcome = State()
    ics_link = State()
    donate = State()
