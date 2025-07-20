from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

delete_router = Router()


@delete_router.message(Command("delete"))
async def help_func(message: Message) -> None:
    await message.reply('Все ваши данные будут удалены!\nВы уверены?')