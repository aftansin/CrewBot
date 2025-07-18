from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

help_router = Router()


@help_router.message(Command("help"))
async def help_func(message: Message) -> None:
    await message.reply('help information!')
