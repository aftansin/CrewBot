import json
import random
from datetime import datetime, timedelta
from hashlib import md5
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Pilot, Event
from utils.calendar import get_calendar_data, get_events_from_calendar, get_most_frequent_user
from db.db_requests import (get_db_pilots, update_pilot_full_name, get_pilot_event_by_id,
                            insert_pilot_event, get_pilot_events_from_yesterday_ascending)
from utils.notify import notify_new_event, notify_updated_event, notify_deleted_event


def calculate_event_hash(event: dict) -> str:
    """Вычисляет хэш для сравнения событий"""
    data = {
        'event_id': event.get('event_id'),
        'summary': event.get('summary'),
        'description': event.get('description'),
        'dtstart': event['dtstart'].isoformat(),
        'dtend': event['dtend'].isoformat()
    }
    return md5(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ОСНОВНАЯ ФУНКЦИЯ
async def check_pilot_calendar(bot: Bot, session: AsyncSession, pilot: Pilot):
    """Основная функция проверки календаря пилота"""
    try:
        # 1. Загружаем текущие события из календаря
        calendar_data = await get_calendar_data(pilot.ics_url)
        if not calendar_data:
            return

        events_dict = await get_events_from_calendar(calendar_data)
        if not events_dict:
            return

        # Обновим ФИО пилота
        pilot_full_name = await get_most_frequent_user(calendar_data)
        if pilot_full_name and not pilot.middle_name:
            await update_pilot_full_name(session, pilot.id, pilot_full_name)

        # 2. Фильтруем события
        now = datetime.now(ZoneInfo('Europe/Moscow'))
        yesterday = now - timedelta(days=1)
        current_month = now.month
        next_month = (now.replace(day=28) + timedelta(days=4)).month

        # Функция для приведения дат к aware
        def ensure_aware(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ZoneInfo('Europe/Moscow'))
            return dt

        # Запишем в бд старые события (с приведением временных зон)
        filtered_events_dict_past_yesterday = [
            e for e in events_dict
            if ensure_aware(e['dtstart']) < yesterday
        ]
        for e in filtered_events_dict_past_yesterday:
            db_event = await get_pilot_event_by_id(session, pilot.id, e['event_id'])
            if not db_event:
                past_event = Event(
                    event_id=e['event_id'],
                    pilot_id=pilot.id,
                    summary=e['summary'],
                    description=e['description'],
                    dtstart=ensure_aware(e['dtstart']),
                    dtend=ensure_aware(e['dtend']),
                    hash=calculate_event_hash(e)
                )
                await insert_pilot_event(session, pilot.id, past_event)

        # Фильтруем будущие события
        filtered_events_dict_future_from_yesterday = [
            e for e in events_dict
            if ensure_aware(e['dtstart']).month in (current_month, next_month) and
               ensure_aware(e['dtstart']) >= yesterday
        ]

        # 3. Получаем существующие события из БД (с приведением временных зон)
        pilot_events = await get_pilot_events_from_yesterday_ascending(session, pilot.id)
        db_existing_events_ids = {
            e.event_id: e
            for e in pilot_events
            if ensure_aware(e.dtstart) >= yesterday
        }

        # 4. Обрабатываем изменения
        for cal_event_dict in filtered_events_dict_future_from_yesterday:
            cal_event_id = cal_event_dict['event_id']
            cal_event_hash = calculate_event_hash(cal_event_dict)

            # Приводим даты к aware
            cal_event_dict['dtstart'] = ensure_aware(cal_event_dict['dtstart'])
            cal_event_dict['dtend'] = ensure_aware(cal_event_dict['dtend'])

            # 4.1. Если событие новое
            if cal_event_id not in db_existing_events_ids:
                new_event = Event(
                    event_id=cal_event_id,
                    pilot_id=pilot.id,
                    summary=cal_event_dict['summary'],
                    description=cal_event_dict['description'],
                    dtstart=cal_event_dict['dtstart'],
                    dtend=cal_event_dict['dtend'],
                    hash=cal_event_hash
                )
                await insert_pilot_event(session, pilot.id, new_event)
                await notify_new_event(bot, pilot.id, new_event)
                continue

            # 4.2. Если событие изменилось
            db_event = db_existing_events_ids[cal_event_id]
            if db_event.hash != cal_event_hash:
                old_values = {
                    'summary': db_event.summary,
                    'description': db_event.description,
                    'dtstart': ensure_aware(db_event.dtstart),
                    'dtend': ensure_aware(db_event.dtend)
                }

                # Обновляем запись в БД
                db_event.summary = cal_event_dict['summary']
                db_event.description = cal_event_dict['description']
                db_event.dtstart = cal_event_dict['dtstart']
                db_event.dtend = cal_event_dict['dtend']
                db_event.hash = cal_event_hash

                await notify_updated_event(bot, pilot.id, old_values, cal_event_dict)

        # 5. Проверяем удаленные события
        cal_current_ids = {e['event_id'] for e in filtered_events_dict_future_from_yesterday}
        for db_event_id, db_event in db_existing_events_ids.items():
            if (db_event_id not in cal_current_ids and
                    ensure_aware(db_event.dtstart).month == now.month):
                await notify_deleted_event(bot, pilot.id, db_event)
                await session.delete(db_event)

        await session.commit()

    except Exception as error:
        logger.error(f"Error in check_pilot_calendar: {error}")
        await session.rollback()


async def start_pilot_calendar_polling(bot, session, scheduler, pilot):
    scheduler.add_job(
        check_pilot_calendar,
        IntervalTrigger(seconds=random.randint(4500, 7200)),
        kwargs={'bot': bot, 'session': session, 'pilot': pilot},
        id=f'{pilot.id}_calendar_polling',
        replace_existing=True
    )


async def remove_pilot_calendar_polling_job(scheduler, pilot):
    user_job = scheduler.get_job(f'{pilot.id}_calendar_polling')
    if user_job:
        logger.debug(f'User job {user_job} removed')
        scheduler.remove_job(f'{pilot.id}_calendar_polling')


async def start_all_calendar_polling(bot, session, scheduler):
    # достанем всех пилотов из бд
    pilots = await get_db_pilots(session)
    for pilot in pilots:
        if pilot.ics_url:
            await start_pilot_calendar_polling(bot, session, scheduler, pilot)
