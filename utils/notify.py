import re
from datetime import datetime
from itertools import zip_longest
from zoneinfo import ZoneInfo

from aiogram import Bot

from db.models import Event


# Вспомогательные функции
def format_datetime(dt: datetime) -> str:
    """Форматирует datetime для сообщений"""
    return dt.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')


def format_crew_member(member: dict) -> str:
    """Форматирует информацию о члене экипажа"""
    name = f"{member['last_name']} {member['first_name']}"
    if member['middle_name']:
        name += f" {member['middle_name']}"
    return f"{name} ({member['position']})"


def remove_crew_from_description(desc: str) -> str:
    """Удаляет блок с экипажем из описания"""
    return re.sub(r'\n.*\([КВС2ПБ].*$', '', desc, flags=re.MULTILINE)


def extract_crew_with_positions(description: str) -> list[dict]:
    """Извлекает ФИО и должности членов экипажа"""
    pattern = r"([А-ЯЁ][а-яё]+)\s+([А-ЯЁ][а-яё]+)(?:\s+([А-ЯЁ][а-яё]+))?\s*\(([^)]+)\)"
    matches = re.findall(pattern, description)

    return [{
        'last_name': last,
        'first_name': first,
        'middle_name': mid if mid else None,
        'position': pos
    } for last, first, mid, pos in matches]


async def notify_new_event(bot: Bot, chat_id: int, event: Event):
    """Уведомление о новом событии"""
    message = (
        "Добавлен новый полет:\n"
        f"<b>{event.summary}</b>\n"
        f"📅 {format_datetime(event.dtstart)} - {format_datetime(event.dtend)}\n\n"
        f"<pre>{event.description}</pre>"
    )
    await bot.send_message(chat_id, message, parse_mode="HTML")


async def notify_updated_event(bot: Bot, chat_id: int, old_values: dict, new_event: dict):
    """Уведомление об изменении события с детальным сравнением описания"""
    changes = []

    # Вспомогательная функция для сравнения текста
    def compare_text(old: str, new: str, field_name: str) -> bool:
        if old.strip() != new.strip():
            changes.append(
                f"📝 {field_name}:\n"
                f"<code>{old[:200] + ('...' if len(old) > 200 else '')}</code>\n→\n"
                f"<code>{new[:200] + ('...' if len(new) > 200 else '')}</code>"
            )
            return True
        return False

    # Проверяем изменения для каждого поля
    for field in ['summary', 'dtstart', 'dtend']:
        old_val = old_values[field]
        new_val = new_event[field]

        if old_val != new_val:
            if field in ('dtstart', 'dtend'):
                changes.append(
                    f"🕒 {field}: {format_datetime(old_val)} → {format_datetime(new_val)}"
                )
            else:
                compare_text(str(old_val), str(new_val), field)

    # Специальная обработка для description (сравнение экипажа)
    old_desc = old_values['description']
    new_desc = new_event['description']

    if old_desc != new_desc:
        # Извлекаем экипаж из старого и нового описания
        old_crew = extract_crew_with_positions(old_desc)
        new_crew = extract_crew_with_positions(new_desc)

        # Если состав экипажа изменился
        if old_crew != new_crew:
            crew_changes = []

            # Проверяем изменения по каждому члену экипажа
            for i, (old_member, new_member) in enumerate(zip_longest(old_crew, new_crew, fillvalue=None)):
                if old_member is None:
                    crew_changes.append(f"➕ Добавлен: {format_crew_member(new_member)}")
                elif new_member is None:
                    crew_changes.append(f"➖ Удален: {format_crew_member(old_member)}")
                elif old_member != new_member:
                    changes_str = []
                    for key in ['last_name', 'first_name', 'middle_name', 'position']:
                        if old_member.get(key) != new_member.get(key):
                            changes_str.append(
                                f"{key}: {old_member.get(key)}→{new_member.get(key)}"
                            )
                    crew_changes.append(
                        f"✏️ Изменен: {format_crew_member(old_member)}\n" +
                        "Изменения: " + ", ".join(changes_str)
                    )

            changes.append("👥 Изменения в экипаже:\n" + "\n".join(crew_changes))

        # Проверяем остальные изменения в описании (не экипаж)
        old_text_without_crew = remove_crew_from_description(old_desc)
        new_text_without_crew = remove_crew_from_description(new_desc)

        compare_text(old_text_without_crew, new_text_without_crew, "description (детали полета)")

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