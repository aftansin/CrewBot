import base64
from datetime import date

from aiogram import Router, Bot, html
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db import Person
from middlewares import RegistrationCheck

person_router = Router()
person_router.message.middleware(RegistrationCheck())


def get_age(birthdate):
    today = date.today()
    current_age = today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))
    return current_age


@person_router.message(Command("person"))
async def command_get_person(message: Message,
                             command: CommandObject,
                             bot: Bot,
                             session_maker: async_sessionmaker[AsyncSession]):
    if command.args:
        full_name = html.quote(command.args).split()
        if len(full_name) < 2:
            await message.reply("Пожалуйста, укажи имя и фамилию после команды /person!")
            return
        for i, word in enumerate(full_name):
            full_name[i] = ''.join(letter for letter in word if letter.isalpha())
        f_name, l_name = full_name[0].title(), full_name[1].title()
        async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=bot):
            async with session_maker() as session:
                async with session.begin():
                    stmt = select(Person).filter_by(first_name=f_name, last_name=l_name).order_by(desc("personnel_id"))
                    response = await session.execute(stmt)
                    person = response.scalar_one_or_none()
                    if not person:
                        await message.reply(f'Не удалось найти\nИмя: {f_name}\nФамилия: {l_name}')
                        return
                    photo = person.photo
                    age = get_age(person.birth_date) if person.birth_date else None
                    caption = (f'<b>{person.last_name}\n{person.first_name}\n{person.middle_name}</b>\n'
                               f'Осн. телефон: {person.phone1}\nДоп. телефон: {person.phone2}\n<u>'
                               f'{person.position}</u>\nДата рождения: {person.birth_date}\nВозраст: {age}')
                    if photo:
                        photo = base64.b64decode(photo)
                        await message.answer_photo(BufferedInputFile(photo, filename="image.jpg"), caption=caption)
                    else:
                        await message.answer(caption)
    else:
        await message.reply("Пожалуйста, укажи имя и фамилию после команды /person!")
