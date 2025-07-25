import re
from collections import Counter
from pprint import pprint
from typing import Optional, Tuple

import requests
from fake_useragent import UserAgent
from icalendar import Calendar


# Базовая функция загрузки данных календаря. Возвращает календарь или None.
def get_calendar_data(url: str):
    ua = UserAgent()
    headers = {
        "Accept": "text/calendar",  # Важно для ICS
        "Accept-Language": "ru-RU",  # Предпочитаем русский язык
        "User-Agent": 'iOS/17.0 (iPhone) CalendarAgent/185'
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        # Загружаем календарь
        ics_content = response.text
        print(ics_content)
        calendar_data = Calendar.from_ical(ics_content)
        if calendar_data.walk("VEVENT"):
            return calendar_data
        else:
            print('Календарь пустой')
    else:
        print("Ошибка:", response.status_code, response.text)
    return None


# Парсинг событий календаря. Возвращает словарь событий или None.
def get_events_from_calendar(calendar_data):
    # Если календарь пустой, то вернем None
    if not calendar_data:
        return None
    events = list()
    for event in calendar_data.walk("VEVENT"):
        event_id = event.get("UID")  # ID
        summary = event.get("summary")  # Событие
        description = event.get("description")  # Описание
        dtstart = event.get("dtstart").dt  # Начало
        dtend = event.get("dtend").dt  # Конец
        events.append({'event_id': int(event_id),
                       'summary': str(summary),
                       'description': str(description),
                       'dtstart': dtstart,
                       'dtend': dtend})
    return events


# Парсинг календаря. Возвращает tuple ФИО календаря или None.
def get_most_frequent_user(calendar_data) -> Optional[Tuple[str, str, str]]:
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


if __name__ == "__main__":
    link = "https://crew.aeroflot.ru/api/calendar/ics/3914d36a-311a-467f-9c9c-1841639cbde0"
    calendar = get_calendar_data(link)
    user_events = get_events_from_calendar(calendar)
    user_name = get_most_frequent_user(calendar)
    pprint(user_events)




