"""Тесты парсера ленты.

Фикстура ``fixtures/feed_sample.ics`` повторяет структуру реальной выгрузки
провайдера, но ФИО экипажа в ней вымышленные.

Зафиксированы четыре особенности формата, на которых спотыкался первый
вариант парсера:

1. ``X-WR-CALNAME`` — название расписания, а не ФИО владельца;
2. ФИО латиницей, должности кириллицей;
3. эмодзи-маркер типа события стоит первым символом SUMMARY;
4. SUMMARY бывает многострочным.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.models import EventKind
from app.icalendar_feed.parse import (
    classify,
    format_crew,
    month_bounds,
    parse_aircraft,
    parse_feed,
    parse_flight_route,
)

TZ = ZoneInfo("Europe/Moscow")
FIXTURE = Path(__file__).parent / "fixtures" / "feed_sample.ics"


def load():
    return parse_feed(FIXTURE.read_text(encoding="utf-8"), TZ)


def by_uid(events):
    return {e.uid: e for e in events}


def test_all_events_parsed():
    events, _owner = load()
    assert len(events) == 7


def test_owner_detected_from_crew_not_calendar_name():
    """X-WR-CALNAME содержит "Aeroflot schedule" — имя надо брать из экипажей."""
    _events, owner = load()
    assert owner == ("Pervyi", "Ivan", "Ivanovich")


def test_kinds_are_classified():
    events, _ = load()
    kinds = {uid: e.kind for uid, e in by_uid(events).items()}
    assert kinds["6217323"] is EventKind.FLIGHT
    assert kinds["1210295"] is EventKind.REPORTING   # 📋 Reporting
    assert kinds["2562558"] is EventKind.REST        # 🏝 Отпуск
    assert kinds["907252"] is EventKind.TRAINING     # 👨‍🎓 Обучение
    assert kinds["3001"] is EventKind.MEDICAL        # 💉 ВЛЭК


def test_emoji_marker_survives_until_classification():
    """Очистка строки не должна съедать маркер до того, как он сработает."""
    assert classify("\U0001f4cb Reporting\n на курсы") is EventKind.REPORTING
    assert classify("\U0001f3dd \u041e\u0442\u043f\u0443\u0441\u043a, 14 дней") is EventKind.REST


def test_latin_crew_is_extracted():
    """Первая версия регулярки была кириллической и находила ноль членов экипажа."""
    events, _ = load()
    flight = by_uid(events)["6217323"]
    assert flight.crew == [
        "Pervyi Ivan Ivanovich (КВС)",
        "Vtoroi Petr Petrovich (2П)",
        "Tretya Olga Olegovna (СБ)",
    ]


def test_airport_names_are_not_mistaken_for_crew():
    """"Sheremetyevo (B)" и "Cairo (CAI | ...)" не должны попасть в экипаж."""
    events, _ = load()
    flight = by_uid(events)["6220629"]
    assert len(flight.crew) == 2
    assert all("Sheremetyevo" not in member for member in flight.crew)
    assert all("Cairo" not in member for member in flight.crew)


def test_flight_route_parsed():
    events, _ = load()
    flight = by_uid(events)["6217323"]
    assert flight.flight_no == "SU1562"
    assert flight.dep_code == "SVO"
    assert flight.dep_city == "Moscow"
    assert flight.arr_code == "IKT"
    assert flight.arr_city == "Irkutsk"


def test_aircraft_type_extracted():
    events, _ = load()
    assert by_uid(events)["6217323"].aircraft == "B-737-800H"
    assert by_uid(events)["2562558"].aircraft is None


def test_multiline_summary_keeps_first_line_as_title():
    events, _ = load()
    reporting = by_uid(events)["1210295"]
    lines = reporting.summary.splitlines()
    assert lines[0].endswith("Reporting")
    assert len(lines) > 1


def test_all_dates_are_timezone_aware():
    events, _ = load()
    assert all(e.dtstart.tzinfo is not None for e in events)
    assert all(e.dtend.tzinfo is not None for e in events)


def test_tzid_from_feed_is_respected():
    events, _ = load()
    flight = by_uid(events)["6217323"]
    assert flight.dtstart == datetime(2026, 9, 15, 1, 30, tzinfo=TZ)
    assert flight.dtend == datetime(2026, 9, 15, 7, 10, tzinfo=TZ)


def test_flight_minutes_sum():
    events, _ = load()
    minutes = sum(
        int((e.dtend - e.dtstart).total_seconds() // 60)
        for e in events
        if e.kind is EventKind.FLIGHT
    )
    # 5:40 + 5:55 + 5:45
    assert minutes == 340 + 355 + 345


def test_content_hash_ignores_whitespace_noise():
    events, _ = load()
    flight = by_uid(events)["6217323"]
    baseline = flight.content_hash()

    lines = [line.strip() for line in flight.description.splitlines() if line.strip()]
    flight.description = "\n\n".join(f"  {line}  " for line in reversed(lines))
    assert flight.content_hash() == baseline


def test_content_hash_reacts_to_crew_change():
    events, _ = load()
    flight = by_uid(events)["6217323"]
    baseline = flight.content_hash()

    flight.description = flight.description.replace("Vtoroi Petr Petrovich", "Pyatyi Semen Semenovich")
    assert flight.content_hash() != baseline


def test_route_without_arrow_is_tolerated():
    result = parse_flight_route("SU100 без маршрута")
    assert result["flight_no"] == "SU100"
    assert result["dep_code"] is None


def test_aircraft_absent_returns_none():
    assert parse_aircraft("обычный текст без скобок") is None


def test_crew_helper_handles_empty_description():
    assert format_crew("") == []
    assert format_crew(None) == []


def test_unknown_event_falls_back_to_other():
    assert classify("Нечто непонятное без маркеров") is EventKind.OTHER


def test_month_bounds_crosses_year():
    december = datetime(2026, 12, 15, 12, 0, tzinfo=TZ)
    start, end = month_bounds(december, TZ, offset=1)
    assert start == datetime(2027, 1, 1, tzinfo=TZ)
    assert end == datetime(2027, 2, 1, tzinfo=TZ)

    start, _ = month_bounds(december, TZ, offset=-1)
    assert start == datetime(2026, 11, 1, tzinfo=TZ)


def test_broken_event_is_skipped_not_fatal():
    raw = FIXTURE.read_text(encoding="utf-8").replace("UID:3001\n", "")
    events, _ = parse_feed(raw, TZ)
    assert len(events) == 6
