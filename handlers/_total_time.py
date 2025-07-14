from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from middlewares import RegistrationCheck
from sql_image import get_top_person_month_flight_time_png, get_top_person_year_flight_time_png

total_time_router = Router()
total_time_router.message.middleware(RegistrationCheck())


@total_time_router.message(Command("get_top_month_time"))
async def command_get_top_month_time(message: Message, bot: Bot) -> None:
    async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=bot):
        img = get_top_person_month_flight_time_png(limit=50)
        await message.answer_photo(BufferedInputFile(img, filename="image.jpg"), caption="Топ 50 налет за месяц")


@total_time_router.message(Command("get_top_month_time_tmp"))
async def command_get_top_month_time(message: Message, bot: Bot, session_maker: async_sessionmaker[AsyncSession]):
    async with session_maker() as session:
        async with session.begin():
            def format_time(delta):
                """Вспомогательная функция. Конвертирует timedelta в %H:%M."""
                totsec = delta.total_seconds()
                h = int(totsec // 3600)
                m = int((totsec % 3600) // 60)
                return f"{h:02d}:{m:02d}"
            query = ("SELECT personnel_id, table_number, CONCAT(last_name, ' ', LEFT(first_name, 1), "
                     "'. ', LEFT(middle_name, 1), '.') AS uname, person.position, person.birth_date, "
                     "SEC_TO_TIME(SUM(TIME_TO_SEC(TIMEDIFF(arrival_time_utc, "
                     "departure_time_utc)))) AS total_time, aircraft_type FROM flight LEFT JOIN flight_person ON "
                     "flight.record_id = flight_person.flight_record_id LEFT JOIN person ON "
                     "person.personnel_id = flight_person.crew_personnel_id "
                     "WHERE flight_person.crew_type = 0 "
                     "AND departure_time_utc BETWEEN '2023-10-01' AND '2023-10-30' "
                     "AND aircraft_type LIKE '%7%'"
                     "GROUP BY personnel_id ORDER BY total_time DESC LIMIT 20;")
            response = await session.execute(text(query))
            result = response.all()
            msg = str()
            for i in result:
                msg += f'{format_time(i.total_time)} - {i.uname}\n'
            await message.answer(msg)


@total_time_router.message(Command("get_top_year_time"))
async def command_get_top_month_time(message: Message, bot: Bot) -> None:
    async with ChatActionSender.upload_photo(chat_id=message.chat.id, bot=bot):
        img = get_top_person_year_flight_time_png(limit=50)
        await message.answer_photo(BufferedInputFile(img, filename="image.jpg"), caption="Топ 50 налет за год")

