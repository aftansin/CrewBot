import json
import logging
from datetime import datetime, timezone, timedelta
from hashlib import md5
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Pilot, Event
from utils.calendar import get_calendar_data, get_events_from_calendar, get_most_frequent_user
from db.db_requests import (get_db_pilots, update_pilot_full_name, get_pilot_event_by_id,
                            insert_pilot_event, get_pilot_events_from_yesterday_ascending)


# Инициализируем логгер модуля
logger = logging.getLogger(__name__)


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
    pass
    """Основная функция проверки календаря пилота"""
    # 1. Загружаем текущие события из календаря
    calendar_data = await get_calendar_data(pilot.ics_url)
    if not calendar_data:  # если нет записей вообще, то ничего делать не будем
        return

    events_dict = await get_events_from_calendar(calendar_data)
    if not events_dict:
        return

    # Обновим ФИО пилота в базе из данных календаря
    pilot_full_name = await get_most_frequent_user(calendar_data)
    if pilot_full_name and not pilot.middle_name:
        await update_pilot_full_name(session, pilot.id, pilot_full_name)

    # 2. Фильтруем события: начиная со вчерашнего дня
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    yesterday = now - timedelta(days=1)

    # Запишем в бд старые события
    filtered_events_dict_past_yesterday = [e for e in events_dict if e['dtstart'] < yesterday]
    for e in filtered_events_dict_past_yesterday:
        db_event = await get_pilot_event_by_id(session, pilot.id, e['event_id'])
        if not db_event:
            past_event = Event(
                event_id=e['event_id'],
                pilot_id=pilot.id,
                summary=e['summary'],
                description=e['description'],
                dtstart=e['dtstart'],
                dtend=e['dtend'],
                hash=calculate_event_hash(e)
            )
            await insert_pilot_event(session, pilot.id, past_event)

    filtered_events_dict_future_from_yesterday = [e for e in events_dict if e['dtstart'] >= yesterday]
    # 3. Получаем существующие события из БД
    pilot_events = await get_pilot_events_from_yesterday_ascending(session, pilot.id)
    db_existing_events_ids = {e.event_id: e for e in pilot_events if e.dtstart >= yesterday}
    # TODO 1-го числа месяца вчерашние события уже не будут отображаться в календаре (продумать)

    # 4. Обрабатываем изменения
    for cal_event_dict in filtered_events_dict_future_from_yesterday:
        cal_event_id = cal_event_dict['event_id']
        cal_event_hash = calculate_event_hash(cal_event_dict)

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
        # Если есть под этим id событие, то сравним их хэш
        if db_event.hash != cal_event_hash:
            old_values = {
                'summary': db_event.summary,
                'description': db_event.description,
                'dtstart': db_event.dtstart,
                'dtend': db_event.dtend
            }

            # Обновляем запись в БД
            db_event.summary = cal_event_dict['summary']
            db_event.description = cal_event_dict['description']
            db_event.dtstart = cal_event_dict['dtstart']
            db_event.dtend = cal_event_dict['dtend']
            db_event.hash = cal_event_hash
            db_event.last_updated = datetime.now()
            # TODO Нужно ли тут обращаться в бд для записи события или он автоматически записывается в бд?
            await notify_updated_event(bot, pilot.id, old_values, cal_event_dict)

    # 5. Проверяем удаленные события (только для текущего месяца)
    cal_current_ids = {e['event_id'] for e in filtered_events_dict_future_from_yesterday}
    for db_event_id, db_event in db_existing_events_ids.items():
        if db_event_id not in cal_current_ids and db_event.dtstart.month == now.month:
            await notify_deleted_event(bot, pilot.id, db_event)
            await session.delete(db_event)

    await session.commit()


def format_datetime(dt: datetime) -> str:
    """Форматирует datetime для сообщений"""
    return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')


async def notify_new_event(bot: Bot, chat_id: int, event: Event):
    """Уведомление о новом событии"""
    message = (
        "✈️ Добавлен новый полет:\n"
        f"<b>{event.summary}</b>\n"
        f"📅 {format_datetime(event.dtstart)} - {format_datetime(event.dtend)}\n"
        f"{event.description[:200]}..."
    )
    await bot.send_message(chat_id, message, parse_mode="HTML")


async def notify_updated_event(bot: Bot, chat_id: int, old_values: dict, new_event: dict):
    """Уведомление об изменении события"""
    changes = []

    for field in ['summary', 'description', 'dtstart', 'dtend']:
        old_val = old_values[field]
        new_val = new_event[field]

        if old_val != new_val:
            if field in ('dtstart', 'dtend'):
                changes.append(
                    f"🕒 {field}: {format_datetime(old_val)} → {format_datetime(new_val)}"
                )
            else:
                changes.append(
                    f"📝 {field}:\n" +
                    f"<code>{old_val}</code>\n→\n<code>{new_val}</code>"
                )

    if changes:
        message = (
                "🔄 Изменения в полете:\n"
                f"<b>{new_event['summary']}</b>\n\n" +
                "\n".join(changes)
        )
        await bot.send_message(chat_id, message, parse_mode="HTML")


async def notify_deleted_event(bot: Bot, chat_id: int, event: Event):
    """Уведомление об удалении события"""
    message = (
        "❌ Полет отменен:\n"
        f"<b>{event.summary}</b>\n"
        f"Был запланирован на {format_datetime(event.dtstart)}"
    )
    await bot.send_message(chat_id, message, parse_mode="HTML")


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
        logger.debug('User job {user_job} removed')
        scheduler.remove_job(f'{pilot.id}_calendar_polling')


async def start_all_calendar_polling(bot, session, scheduler):
    # достанем всех пилотов из бд
    pilots = await get_db_pilots(session)
    for pilot in pilots:
        if pilot.ics_url:
            await start_pilot_calendar_polling(bot, session, scheduler, pilot)
