"""Тесты диффера.

Это единственное место, где ошибка стоит дорого и почти не проверяется
руками: чтобы увидеть баг с отменами, нужно дождаться реальной отмены
рейса. Поэтому сценарии проиграны на подделанных данных.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db.models import Event, EventKind, EventStatus
from app.icalendar_feed.types import ParsedEvent
from app.sync.differ import build_diff, guard_tripped

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 12, 10, 0, tzinfo=TZ)


def parsed(uid: str, day: int, month: int = 9, hour: int = 8, summary: str = "SU100") -> ParsedEvent:
    start = datetime(2026, month, day, hour, 0, tzinfo=TZ)
    return ParsedEvent(
        uid=uid,
        kind=EventKind.FLIGHT,
        summary=summary,
        description="Иванов Иван Иванович (КВС)",
        dtstart=start,
        dtend=start + timedelta(hours=3),
    )


def stored(source: ParsedEvent, status: EventStatus = EventStatus.SCHEDULED) -> Event:
    event = Event(
        id=abs(hash(source.uid)) % 10_000,
        pilot_id=1,
        uid=source.uid,
        kind=source.kind,
        status=status,
        summary=source.summary,
        description=source.description,
        location=source.location,
        dtstart=source.dtstart,
        dtend=source.dtend,
        content_hash=source.content_hash(),
    )
    return event


def test_new_event_detected():
    feed = [parsed("a", 15), parsed("b", 16)]
    db = [stored(parsed("a", 15))]

    diff = build_diff(feed, db, NOW)

    assert [e.uid for e in diff.created] == ["b"]
    assert not diff.updated
    assert not diff.cancelled


def test_time_shift_reported_as_update():
    original = parsed("a", 15, hour=8)
    shifted = parsed("a", 15, hour=11)
    diff = build_diff([shifted], [stored(original)], NOW)

    assert len(diff.updated) == 1
    fields = {c.field for c in diff.updated[0].changes}
    assert fields == {"dtstart", "dtend"}


def test_description_reordering_is_not_a_change():
    """Провайдер тасует строки в описании — это не изменение плана."""
    original = parsed("a", 15)
    original.description = "Иванов Иван (КВС)\nПетров Пётр (2П)"
    same = parsed("a", 15)
    same.description = "Петров Пётр (2П)\n  Иванов Иван (КВС)  "

    diff = build_diff([same], [stored(original)], NOW)

    assert not diff.updated
    assert len(diff.unchanged) == 1


def test_crew_change_is_an_update():
    original = parsed("a", 15)
    original.description = "Иванов Иван (КВС)\nПетров Пётр (2П)"
    changed = parsed("a", 15)
    changed.description = "Иванов Иван (КВС)\nСидоров Семён (2П)"

    diff = build_diff([changed], [stored(original)], NOW)

    assert len(diff.updated) == 1
    assert {c.field for c in diff.updated[0].changes} == {"description"}


def test_cancellation_inside_feed_window():
    a, b = parsed("a", 15), parsed("b", 20)
    diff = build_diff([a], [stored(a), stored(b)], NOW)
    # b внутри окна ленты? Окно = [15.09, 15.09], b=20.09 вне его.
    assert not diff.cancelled

    c = parsed("c", 25)
    diff = build_diff([a, c], [stored(a), stored(b), stored(c)], NOW)
    # Теперь окно [15.09, 25.09], b=20.09 внутри и пропал -> отмена.
    assert [e.uid for e in diff.cancelled] == ["b"]


def test_next_month_cancellation_detected():
    """Главный баг старой версии: отмены на следующий месяц терялись.

    Старое условие требовало ``dtstart.month == now.month``, поэтому
    октябрьское событие, пропавшее из ленты в сентябре, игнорировалось.
    """
    sep = parsed("sep", 20, month=9)
    oct_a = parsed("oct_a", 5, month=10)
    oct_b = parsed("oct_b", 25, month=10)

    diff = build_diff([sep, oct_b], [stored(sep), stored(oct_a), stored(oct_b)], NOW)

    assert [e.uid for e in diff.cancelled] == ["oct_a"]


def test_past_events_are_never_cancelled():
    """С 1-го числа прошлый месяц пропадает из ленты — это не отмена."""
    august = parsed("aug", 10, month=8)
    september = parsed("sep", 20, month=9)

    diff = build_diff([september], [stored(august), stored(september)], NOW)

    assert not diff.cancelled


def test_restored_event():
    event = parsed("a", 20)
    db_event = stored(event, status=EventStatus.CANCELLED)

    diff = build_diff([event], [db_event], NOW)

    assert len(diff.restored) == 1
    assert not diff.created


def test_empty_feed_produces_nothing():
    """Пустая лента не должна выглядеть как отмена всего плана."""
    db = [stored(parsed("a", 15)), stored(parsed("b", 20))]

    diff = build_diff([], db, NOW)

    assert not diff.has_changes


def test_guard_trips_on_mass_disappearance():
    feed = [parsed("a", 15)]
    db = [stored(parsed(uid, day)) for uid, day in
          (("a", 15), ("b", 16), ("c", 17), ("d", 18), ("e", 19), ("f", 20))]
    # Расширяем окно, чтобы отмены вообще попали в diff.
    feed.append(parsed("z", 21))
    db.append(stored(parsed("z", 21)))

    diff = build_diff(feed, db, NOW)
    future_count = len([e for e in db if e.dtend >= NOW])

    assert guard_tripped(diff, future_count, ratio=0.5, min_events=4)


def test_guard_silent_on_small_change():
    feed = [parsed("a", 15), parsed("b", 16), parsed("c", 17), parsed("e", 19)]
    db = [stored(parsed(uid, day)) for uid, day in
          (("a", 15), ("b", 16), ("c", 17), ("d", 18), ("e", 19))]

    diff = build_diff(feed, db, NOW)
    future_count = len([e for e in db if e.dtend >= NOW])

    assert [e.uid for e in diff.cancelled] == ["d"]
    assert not guard_tripped(diff, future_count, ratio=0.5, min_events=4)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        build_diff([], [], datetime(2026, 9, 12, 10, 0))
