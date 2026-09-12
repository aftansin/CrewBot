"""Сравнение ленты с базой. Чистая логика, без БД и без сети — поэтому покрыта тестами.

Главные правила:

1. Лента показывает ограниченное окно (текущий и следующий месяц, причём
   с 1-го числа прошлый месяц пропадает). Значит, исчезновение события
   из ленты само по себе НЕ означает отмену. Отменой считается исчезновение
   только внутри окна, которое лента реально покрывает.

2. Окно определяется по самим данным: от минимальной до максимальной даты
   в ленте. Никаких вычислений "текущий месяц плюс один" по номеру месяца —
   именно на этом старая версия теряла отмены на следующий месяц и путала
   январи разных лет.

3. События в прошлом не воскрешаются и не отменяются: они архив.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.db.models import Event, EventStatus
from app.icalendar_feed.types import ParsedEvent


@dataclass(slots=True)
class FieldChange:
    field: str
    old: object
    new: object


@dataclass(slots=True)
class UpdatedEvent:
    stored: Event
    parsed: ParsedEvent
    changes: list[FieldChange]


@dataclass(slots=True)
class Diff:
    created: list[ParsedEvent] = field(default_factory=list)
    updated: list[UpdatedEvent] = field(default_factory=list)
    cancelled: list[Event] = field(default_factory=list)
    restored: list[tuple[Event, ParsedEvent]] = field(default_factory=list)
    unchanged: list[tuple[Event, ParsedEvent]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.created or self.updated or self.cancelled or self.restored)

    @property
    def total_changes(self) -> int:
        return len(self.created) + len(self.updated) + len(self.cancelled) + len(self.restored)


TRACKED_FIELDS = ("summary", "description", "dtstart", "dtend", "location", "kind")


def feed_window(events: list[ParsedEvent]) -> tuple[datetime, datetime] | None:
    """Диапазон дат, реально покрытый лентой."""
    if not events:
        return None
    starts = [e.dtstart for e in events]
    return min(starts), max(starts)


def build_diff(
    parsed_events: list[ParsedEvent],
    stored_events: list[Event],
    now: datetime,
) -> Diff:
    """Сопоставляет ленту с базой.

    ``stored_events`` — все события пилота из БД, включая отменённые.
    ``now`` обязан быть timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now должен быть timezone-aware")

    diff = Diff()
    by_uid: dict[str, Event] = {e.uid: e for e in stored_events}
    window = feed_window(parsed_events)

    for parsed in parsed_events:
        stored = by_uid.get(parsed.uid)

        if stored is None:
            diff.created.append(parsed)
            continue

        if stored.status is EventStatus.CANCELLED:
            # Событие вернулось в ленту — восстанавливаем.
            diff.restored.append((stored, parsed))
            continue

        if stored.content_hash == parsed.content_hash():
            diff.unchanged.append((stored, parsed))
            continue

        changes = _collect_changes(stored, parsed)
        if changes:
            diff.updated.append(UpdatedEvent(stored=stored, parsed=parsed, changes=changes))
        else:
            # Хэш разошёлся, но значимых полей это не коснулось
            # (например, провайдер переставил строки в описании).
            # Обновляем хэш молча, пользователя не дёргаем.
            diff.unchanged.append((stored, parsed))

    if window is not None:
        feed_uids = {e.uid for e in parsed_events}
        window_start, window_end = window
        for stored in stored_events:
            if stored.uid in feed_uids:
                continue
            if stored.status is EventStatus.CANCELLED:
                continue
            if stored.dtend < now:
                # Прошлое. Пропало из ленты просто потому, что устарело.
                continue
            if not (window_start <= stored.dtstart <= window_end):
                # Вне покрытия ленты — судить об отмене нельзя.
                continue
            diff.cancelled.append(stored)

    return diff


def _collect_changes(stored: Event, parsed: ParsedEvent) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for name in TRACKED_FIELDS:
        old = getattr(stored, name)
        new = getattr(parsed, name)
        if name == "description":
            if _normalize(old) != _normalize(new):
                changes.append(FieldChange(name, old, new))
        elif name == "summary":
            if " ".join((old or "").split()) != " ".join((new or "").split()):
                changes.append(FieldChange(name, old, new))
        elif old != new:
            changes.append(FieldChange(name, old, new))
    return changes


def _normalize(text: str | None) -> str:
    return "\n".join(sorted(line.strip() for line in (text or "").splitlines() if line.strip()))


def guard_tripped(
    diff: Diff,
    stored_future_count: int,
    ratio: float,
    min_events: int,
) -> bool:
    """Предохранитель от неполного ответа провайдера.

    Если лента валидна, но обрезана, бот иначе разошлёт "отменено" по всему
    плану. Срабатывает, только когда будущих событий достаточно много,
    чтобы доля имела смысл.
    """
    if stored_future_count < min_events:
        return False
    if not diff.cancelled:
        return False
    return len(diff.cancelled) / stored_future_count > ratio
