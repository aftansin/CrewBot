from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram_dialog import DialogManager, StartMode

from states.start import StartState

start_router = Router()


@start_router.message(CommandStart())
async def handler(msg: Message, dialog_manager: DialogManager):
    await dialog_manager.start(StartState.welcome, mode=StartMode.RESET_STACK)
