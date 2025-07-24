import re
import logging
from collections import Counter
from pprint import pprint
from typing import Optional, Tuple

import aiohttp
from icalendar import Calendar


# Инициализируем логгер модуля
logger = logging.getLogger(__name__)

# Базовая функция загрузки данных календаря. Возвращает календарь или None.
async def get_calendar_data(url: str):
    """Асинхронная загрузка календаря с кешированием заголовков"""
    headers = {
        "Accept": "text/calendar",
        "User-Agent": "iOS/17.0 (iPhone) CalendarAgent/185"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    ics_content = await response.text()
                    print(ics_content)
                    return Calendar.from_ical(ics_content)
                else:
                    logger.error(f"Calendar fetch error: {response.status}")
                    return None

            # with open('utils/test.txt', 'r', encoding='utf-8') as file:
            #     return Calendar.from_ical(file.read())

    except Exception as e:
        logger.error(f"Calendar fetch exception: {e}")
        return None


# Парсинг событий календаря. Возвращает словарь событий или None.
async def get_events_from_calendar(calendar_data):
    # Если календарь пустой, то вернем None
    if not calendar_data:
        logger.warning(f"⚠️ get_events_from_calendar: None")
        return None
    events_dict = list()
    for event in calendar_data.walk("VEVENT"):
        event_id = event.get("UID")  # ID
        summary = event.get("summary")  # Событие
        description = event.get("description")  # Описание
        dtstart = event.get("dtstart").dt  # Начало
        dtend = event.get("dtend").dt  # Конец
        events_dict.append({'event_id': int(event_id),
                       'summary': str(summary),
                       'description': str(description),
                       'dtstart': dtstart,
                       'dtend': dtend})
    sorted_events_dict = sorted(events_dict, key=lambda x: x['dtstart'])
    return sorted_events_dict


# Парсинг календаря. Возвращает tuple ФИО календаря или None.
async def get_most_frequent_user(calendar_data) -> Optional[Tuple[str, str, str]]:
    data_summary = list()
    # Если календарь пустой, то вернем None
    if not calendar_data:
        return None
    for event in calendar_data.walk("VEVENT"):
        description = event.get("description")  # Описание
        data_summary.append(str(description))
    # Регулярное выражение для извлечения имен пользователей
    user_pattern = r'([A-ZА-ЯЁ][a-zа-яё]+)\s+([A-ZА-ЯЁ][a-zа-яё]+)\s+([A-ZА-ЯЁ][a-zа-яё]+)'  # "Фамилия Имя Отчество"
    users = []
    # Проход по всем событиям в списке
    for event in data_summary:
        # Ищем всех пользователей по шаблону
        found_users = re.findall(user_pattern, event)
        users.extend(found_users)  # добавляем найденных пользователей в общий список
    # Используем Counter для подсчета вхождений
    user_counts = Counter(users)
    # Находим пользователя с максимальным количеством вхождений
    most_common_user = user_counts.most_common(1)
    # Если есть такие пользователи, возвращаем имя и количество
    if most_common_user:
        return most_common_user[0][0]  # возвращаем кортеж (ФИО)
    else:
        return None  # если пользователей нет
