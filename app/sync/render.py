"""Формирование сообщений.

Весь текст из ленты проходит через html.escape. Старая версия вставляла
summary и description в <pre> как есть — одна угловая скобка или амперсанд
от провайдера, и Telegram отклонял сообщение целиком.
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db.models import Event, EventKind
from app.icalendar_feed.parse import format_crew
from app.icalendar_feed.types import ParsedEvent
from app.sync.differ import Diff, FieldChange, UpdatedEvent

KIND_LABEL: dict[EventKind, str] = {
    EventKind.FLIGHT: "Рейс",
    EventKind.SIMULATOR: "Тренажёр",
    EventKind.TRAINING: "Учёба",
    EventKind.MEDICAL: "Медкомиссия",
    EventKind.REPORTING: "Явка",
    EventKind.STANDBY: "Резерв",
    EventKind.DEADHEAD: "Перелёт пассажиром",
    EventKind.DUTY: "Наземная работа",
    EventKind.REST: "Отдых",
    EventKind.OTHER: "Событие",
}

KIND_ICON: dict[EventKind, str] = {
    EventKind.FLIGHT: "\u2708\ufe0f",
    EventKind.SIMULATOR: "\U0001f579\ufe0f",
    EventKind.TRAINING: "\U0001f4da",
    EventKind.MEDICAL: "\U0001f489",
    EventKind.REPORTING: "\U0001f4cb",
    EventKind.STANDBY: "\u23f3",
    EventKind.DEADHEAD: "\U0001f9f3",
    EventKind.DUTY: "\U0001f3e2",
    EventKind.REST: "\U0001f6cf\ufe0f",
    EventKind.OTHER: "\U0001f4c5",
}


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def fmt_dt(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def fmt_time(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%H:%M")


def fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60} ч {minutes % 60:02d} мин"


def short_title(event: Event | ParsedEvent) -> str:
    """Компактная подпись для списков и кнопок."""
    if event.kind is EventKind.FLIGHT:
        route = None
        if event.dep_code and event.arr_code:
            route = f"{event.dep_code}\u2013{event.arr_code}"
        elif event.dep_city and event.arr_city:
            route = f"{event.dep_city} \u2192 {event.arr_city}"
        pieces = [p for p in (event.flight_no, route) if p]
        if pieces:
            return " ".join(pieces)
    # SUMMARY бывает многострочным: первая строка — заголовок, дальше детали.
    first_line = next((line for line in event.summary.splitlines() if line.strip()), "")
    title = " ".join(first_line.split())
    return title[:40] + ("\u2026" if len(title) > 40 else "")


def event_line(event: Event, tz: ZoneInfo) -> str:
    icon = KIND_ICON.get(event.kind, "\U0001f4c5")
    local = event.dtstart.astimezone(tz)
    return (
        f"{icon} {local:%d.%m} {fmt_time(event.dtstart, tz)}\u2013"
        f"{fmt_time(event.dtend, tz)}  {esc(short_title(event))}"
    )


def event_card(event: Event, tz: ZoneInfo) -> str:
    lines = [
        f"{KIND_ICON.get(event.kind, '')} <b>{esc(short_title(event))}</b>",
        f"<i>{esc(KIND_LABEL.get(event.kind, 'Событие'))}</i>",
        "",
        f"Начало: <b>{fmt_dt(event.dtstart, tz)}</b>",
        f"Конец:  <b>{fmt_dt(event.dtend, tz)}</b>",
    ]
    if event.kind is EventKind.FLIGHT:
        lines.append(f"Длительность: {fmt_minutes(event.block_minutes)}")
        if event.aircraft:
            lines.append(f"Борт: {esc(event.aircraft)}")
        if event.actual_block_minutes is not None:
            lines.append("<i>(время скорректировано вручную)</i>")
    if event.dep_city and event.arr_city:
        lines.append(f"Маршрут: {esc(event.dep_city)} \u2192 {esc(event.arr_city)}")
    if event.location:
        lines.append(f"Место: {esc(event.location)}")

    detail_lines = event.summary.splitlines()[1:]
    details = " ".join(" ".join(detail_lines).split())
    if details:
        lines.append("")
        lines.append(esc(details))

    crew = format_crew(event.description)
    if crew:
        lines.append("")
        lines.append("<b>Экипаж</b>")
        lines.extend(f"\u2022 {esc(member)}" for member in crew)
    elif event.description:
        lines.append("")
        lines.append(f"<pre>{esc(event.description.strip())}</pre>")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Уведомления об изменениях
# --------------------------------------------------------------------------

FIELD_LABEL = {
    "summary": "Описание",
    "description": "Состав / детали",
    "dtstart": "Начало",
    "dtend": "Конец",
    "location": "Место",
    "kind": "Тип события",
}


def render_diff(diff: Diff, tz: ZoneInfo) -> list[str]:
    """Возвращает список готовых сообщений.

    Разбит на несколько, чтобы не упереться в лимит Telegram в 4096 символов
    при большом обновлении плана.
    """
    blocks: list[str] = []

    if diff.created:
        blocks.append(
            "\U0001f195 <b>Новое в плане</b>\n\n"
            + "\n".join(_new_line(p, tz) for p in diff.created)
        )

    for item in diff.updated:
        blocks.append(_updated_block(item, tz))

    if diff.restored:
        blocks.append(
            "\u267b\ufe0f <b>Вернулось в план</b>\n\n"
            + "\n".join(_new_line(p, tz) for _stored, p in diff.restored)
        )

    if diff.cancelled:
        blocks.append(
            "\u274c <b>Убрано из плана</b>\n\n"
            + "\n".join(
                f"\u2022 <s>{esc(short_title(e))}</s>  {fmt_dt(e.dtstart, tz)}"
                for e in diff.cancelled
            )
        )

    return _pack(blocks)


def _new_line(parsed: ParsedEvent, tz: ZoneInfo) -> str:
    icon = KIND_ICON.get(parsed.kind, "\U0001f4c5")
    return (
        f"{icon} <b>{esc(short_title(parsed))}</b>\n"
        f"    {fmt_dt(parsed.dtstart, tz)} \u2013 {fmt_time(parsed.dtend, tz)}"
    )


def _updated_block(item: UpdatedEvent, tz: ZoneInfo) -> str:
    header = f"\U0001f504 <b>Изменение: {esc(short_title(item.stored))}</b>"
    lines = [header, f"    {fmt_dt(item.stored.dtstart, tz)}", ""]

    for change in item.changes:
        if change.field == "description":
            lines.extend(_crew_change_lines(str(change.old or ""), str(change.new or "")))
        else:
            lines.append(_simple_change_line(change, tz))

    return "\n".join(line for line in lines if line is not None)


def _simple_change_line(change: FieldChange, tz: ZoneInfo) -> str:
    label = FIELD_LABEL.get(change.field, change.field)
    if isinstance(change.old, datetime) and isinstance(change.new, datetime):
        old_text = fmt_dt(change.old, tz)
        new_text = fmt_dt(change.new, tz)
    elif change.field == "kind":
        old_text = KIND_LABEL.get(change.old, str(change.old))  # type: ignore[arg-type]
        new_text = KIND_LABEL.get(change.new, str(change.new))  # type: ignore[arg-type]
    else:
        old_text = " ".join(str(change.old or "\u2014").split())
        new_text = " ".join(str(change.new or "\u2014").split())
    return f"{label}:\n  <s>{esc(old_text)}</s>\n  \u2192 <b>{esc(new_text)}</b>"


def _crew_change_lines(old_description: str, new_description: str) -> list[str]:
    old_crew = set(format_crew(old_description))
    new_crew = set(format_crew(new_description))
    removed = sorted(old_crew - new_crew)
    added = sorted(new_crew - old_crew)

    if not removed and not added:
        return ["Детали события обновлены."]

    lines = ["\U0001f465 Экипаж:"]
    lines.extend(f"  <s>{esc(member)}</s>" for member in removed)
    lines.extend(f"  <b>+ {esc(member)}</b>" for member in added)
    return lines


TELEGRAM_LIMIT = 3800


def _pack(blocks: list[str]) -> list[str]:
    """Склеивает блоки в сообщения, не превышающие лимит Telegram."""
    messages: list[str] = []
    buffer = ""
    for block in blocks:
        if len(block) > TELEGRAM_LIMIT:
            if buffer:
                messages.append(buffer)
                buffer = ""
            messages.extend(
                block[i : i + TELEGRAM_LIMIT] for i in range(0, len(block), TELEGRAM_LIMIT)
            )
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) > TELEGRAM_LIMIT:
            messages.append(buffer)
            buffer = block
        else:
            buffer = candidate
    if buffer:
        messages.append(buffer)
    return messages


# Названия функций пилота для показа. В интерфейсе не должно быть
# внутренних кодов вроде "unverified".
FUNCTION_LABEL = {
    "pic": "КВС",
    "picus": "КВС под надзором",
    "copilot": "Второй пилот",
    "cruise_relief": "Усиленный экипаж",
    "dual": "С инструктором",
    "fi": "Инструктор",
    "fe": "Проверяющий",
    "unverified": "не указана",
}


def function_label(value) -> str:
    code = getattr(value, "value", value)
    return FUNCTION_LABEL.get(code, str(code))
