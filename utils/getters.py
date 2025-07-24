import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.enums import ContentType
from aiogram_dialog import DialogManager
from aiogram_dialog.api.entities import MediaAttachment

from db.db_requests import get_pilot_event_by_id


async def pilot_data_getter(dialog_manager: DialogManager, **middleware_data):
    db_pilot = middleware_data.get('db_pilot')
    return {'username': db_pilot.username,
            'last_name': db_pilot.last_name,
            'first_name': db_pilot.first_name,
            'middle_name': db_pilot.middle_name,
            'registration_date': db_pilot.registration_date.date(),
            'subscription_date': None if not db_pilot.subscription_date else db_pilot.subscription_date.date(),
            'ics_url': db_pilot.ics_url}


async def qr_code_getter(**kwargs):
    img = MediaAttachment(type=ContentType.PHOTO, path='db/QR_Code.jpg')
    return {'qr_code': img}


async def user_events_getter(dialog_manager: DialogManager, **middleware_data):
    events = dialog_manager.dialog_data.get('events', [])

    now = datetime.now(ZoneInfo('Europe/Moscow'))
    current_month_num = now.month
    current_year = now.year

    # Инициализируем переменные для хранения времени
    current_month_duration = timedelta()
    prev_month_duration = timedelta()
    next_month_duration = timedelta()

    for event in events:
        # Пропускаем события без символа ✈️
        if '✈️' not in event.summary:
            continue

        # Приводим dtstart к aware datetime, если он naive
        event_time = event.dtstart
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=ZoneInfo('Europe/Moscow'))

        # Рассчитываем длительность полета
        try:
            duration = event.dtend - event.dtstart
            # Игнорируем отрицательные длительности (ошибки в данных)
            if duration.total_seconds() < 0:
                continue
        except:
            continue

        # Определяем месяц события
        event_month = event_time.month
        event_year = event_time.year

        # Классифицируем по месяцам
        if event_year == current_year:
            if event_month == current_month_num:
                current_month_duration += duration
            elif event_month == current_month_num - 1 or (current_month_num == 1 and event_month == 12):
                prev_month_duration += duration
            elif event_month == current_month_num + 1 or (current_month_num == 12 and event_month == 1):
                next_month_duration += duration

    # Конвертируем timedelta в часы и минуты
    def format_duration(delta):
        total_seconds = delta.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        return f"{hours}ч {minutes}м" if hours or minutes else "0ч 0м"

    return {
        'events': events,
        'current_month_time': format_duration(current_month_duration),
        'previous_month_time': format_duration(prev_month_duration),
        'next_month_time': format_duration(next_month_duration),
        'has_flights': any('✈️' in e.summary for e in events)  # Флаг наличия полетов
    }

async def event_info_getter(dialog_manager: DialogManager, **middleware_data):
    session = middleware_data.get('session')
    context = dialog_manager.current_context()
    event_id = int(context.dialog_data.get('event_id'))
    user_id = middleware_data.get('event_from_user').id
    db_event = await get_pilot_event_by_id(session, user_id, event_id)
    dtstart = db_event.dtstart.astimezone(ZoneInfo('Europe/Moscow')).strftime("%d.%m.%Y  %H:%M (%Z)")
    dtend = db_event.dtend.astimezone(ZoneInfo('Europe/Moscow')).strftime("%d.%m.%Y  %H:%M (%Z)")

    pattern = r"^\s*([А-ЯЁа-яёA-Za-z-]+\s[А-ЯЁа-яёA-Za-z-]+(?:\s[А-ЯЁа-яёA-Za-z-]+)?)\s*\((КВС|2П|СБ)\)"
    # Ищем все совпадения в тексте
    matches = re.findall(pattern, db_event.description, re.MULTILINE)
    crew_list = [f"{name.strip()} ({position})" for name, position in matches]
    crew_str = '\n'.join(crew_list)

    # Проверяем, закончилось ли событие
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    event_end = db_event.dtend.astimezone(ZoneInfo('Europe/Moscow'))
    is_past = now > event_end


    short_summary = db_event.short_summary.split(' ', 1)[1]
    return {'event_id': db_event.event_id,
            'pilot_id': db_event.pilot_id,
            'summary': db_event.summary,
            'crew': crew_str if crew_list else db_event.description,
            'dtstart': dtstart,
            'dtend': dtend,
            'short_summary': short_summary,
            'is_past': is_past}