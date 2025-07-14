import logging

from aiogram import Bot
from apscheduler.triggers.interval import IntervalTrigger

from db.db_requests import get_db_pilots, update_pilot_full_name, get_pilot_event_by_id, insert_pilot_event
from utils.calendar import get_calendar_data, get_events_from_calendar, get_most_frequent_user


# Инициализируем логгер модуля
logger = logging.getLogger(__name__)


# ОСНОВНАЯ ФУНКЦИЯ ВСЕЙ ИДЕИ БОТА
async def check_pilot_calendar(bot: Bot, session, pilot):
    current_calendar_data = await get_calendar_data(pilot.ics_url)
    if not current_calendar_data:  # если нет записей вообще, то ничего делать не будем
        return
    calendar_events = await get_events_from_calendar(current_calendar_data)
    pilot_full_name = await get_most_frequent_user(current_calendar_data)
    # Обновим ФИО пилота в базе из данных календаря
    if not pilot.middle_name:
        await update_pilot_full_name(session, pilot.id, pilot_full_name)

    # Пробежим по текущим событиям календаря и сравним со записями в бд.
    for event in calendar_events:
        current_event_id = event.get('event_id')
        db_event = await get_pilot_event_by_id(session, pilot.id, current_event_id)
        # Если нет в базе вообще события, то запишем в базу и сообщим пользователю
        if not db_event:
            user_msg = f'Новое событие: \n{event.get("summary")}'
            await bot.send_message(pilot.id, user_msg)
            await insert_pilot_event(session, pilot.id, event)
        #


async def start_pilot_calendar_polling(bot, session, scheduler, pilot):
    scheduler.add_job(
        check_pilot_calendar,
        IntervalTrigger(seconds=10),
        kwargs={'bot': bot, 'session': session, 'pilot': pilot},
        id=f'{pilot.id}_calendar_polling',
        replace_existing=True
    )


async def remove_pilot_calendar_polling_job(scheduler, pilot):
    user_job = scheduler.get_job(f'{pilot.id}_calendar_polling')
    if user_job:
        scheduler.remove_job(f'{pilot.id}_calendar_polling')


async def start_all_calendar_polling(bot, session, scheduler):
    # достанем всех пилотов из бд
    pilots = await get_db_pilots(session)
    for pilot in pilots:
        if pilot.ics_url:
            await start_pilot_calendar_polling(bot, session, scheduler, pilot)
