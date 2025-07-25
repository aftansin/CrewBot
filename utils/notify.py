import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from db.models import Event


# Вспомогательные функции
def format_datetime(dt: datetime) -> str:
    """Форматирует datetime для сообщений"""
    return dt.astimezone(ZoneInfo('Europe/Moscow')).strftime("%d.%m.%Y  %H:%M (%Z)")


def strip_summary(summary: str) -> str:
    """Форматирует summary для сообщений, удаляя коды аэропортов и дополнительную информацию в скобках"""
    # Удаляем все блоки в скобках (включая вложенные)
    result = re.sub(r'\([^()]*(?:\|.*?)?\)', '', summary)
    # Удаляем лишние пробелы и одиночные символы (например, оставшиеся |)
    result = re.sub(r'\s+', ' ', result).strip()
    # Удаляем пробелы вокруг стрелки
    result = re.sub(r'\s*→\s*', ' → ', result)
    return result


def extract_crew_with_positions(description: str) -> list[str]:
    """Извлекает ФИО и должности членов экипажа из сложного текста"""
    pattern = r"([А-ЯЁ][А-ЯЁа-яё-]+)\s+([А-ЯЁ][А-ЯЁа-яё-]+)(?:\s+([А-ЯЁ][А-ЯЁа-яё-]+))?\s*\((КВС|2П|СБ)\)"
    matches = re.findall(pattern, description)
    return [f"{last} {first} {mid + ' ' if mid else ''}({pos})" for last, first, mid, pos in matches]


async def notify_new_event(bot: Bot, chat_id: int, event: Event):
    """Уведомление о новом событии"""
    message = (
        "<b>⚠️ New event:</b>\n"
        f"<pre>{event.summary}</pre>\n"
        f"• ↗️ {format_datetime(event.dtstart)}\n"
        f"• ↘️ {format_datetime(event.dtend)}\n\n"
        f"<pre>{event.description}</pre>"
    )
    await bot.send_message(chat_id, message, parse_mode="HTML")


async def notify_updated_event(bot: Bot, chat_id: int, old_values: dict, new_event: dict):
    """Уведомление об изменении события с детальным сравнением описания"""
    changes = []
    # Проверяем изменения для каждого поля
    for field in ['summary', 'description', 'dtstart', 'dtend']:
        old_val = old_values[field]
        new_val = new_event[field]

        if field == 'summary':
            if old_val.strip() != new_val.strip():
                changes.append(f"📝 <b><s>{strip_summary(old_val)}</s></b>\n"
                               f"  ↳ <b>{strip_summary(new_val)}</b>\n")
            else:
                changes.append(f"<b>{strip_summary(old_val)}</b>\n")

        elif field == 'dtstart':
            if old_val != new_val:
                changes.append(f"↗️  <s>{format_datetime(old_val)}</s>\n"
                               f"  ↳ {format_datetime(new_val)}")
        elif field == 'dtend':
            if old_val != new_val:
                changes.append(f"↘️  <s>{format_datetime(old_val)}</s>\n"
                               f"  ↳ {format_datetime(new_val)}")
        elif field == 'description':
            # Специальная обработка для description (сравнение экипажа)
            old_desc = old_values.get('description', '')
            new_desc = new_event.get('description', '')

            if old_desc != new_desc:
                old_crew = extract_crew_with_positions(old_desc)
                new_crew = extract_crew_with_positions(new_desc)

                # Создаем множества для сравнения
                old_set = set(old_crew)
                new_set = set(new_crew)

                # Находим различия
                removed = old_set - new_set
                added = new_set - old_set

                if removed or added:
                    crew_changes = []
                    if removed:
                        crew_changes.append("\n".join(f"• <s>{m}</s>" for m in removed))
                    if added:
                        crew_changes.append("\n".join(f"• {m}" for m in added))

                    changes.append("👥 Crew change:\n" + "\n".join(crew_changes))

    if changes:
        message = (
                "🔄 <b>Changes:</b>\n"
                + "\n".join(changes)
        )
        await bot.send_message(chat_id, message, parse_mode="HTML")


async def notify_deleted_event(bot: Bot, chat_id: int, event: Event):
    """Уведомление об удалении события"""
    message = (
        "❌ <b>Event deleted:</b>\n"
        f"<pre>{event.summary}</pre>\n"
        f"📅 {format_datetime(event.dtstart)}"
    )
    await bot.send_message(chat_id, message, parse_mode="HTML")
