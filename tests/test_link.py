"""Тесты связи записи с планом: снимок, расхождения, форс-мажор."""
from __future__ import annotations

from datetime import UTC, datetime

from app.db.models import Event, EventKind, EventStatus, Flight
from app.logbook.link import compare_actual, compare_to_plan, event_was_cancelled, snapshot_of


def make_event(**kw) -> Event:
    d = dict(uid="1", kind=EventKind.FLIGHT, summary="SU400 Moscow -> Cairo",
             description="", dtstart=datetime(2026, 9, 21, 9, 45, tzinfo=UTC),
             dtend=datetime(2026, 9, 21, 15, 30, tzinfo=UTC), flight_no="SU400",
             dep_code="SVO", arr_code="CAI", aircraft="B-737-800H",
             content_hash="h1", pilot_id=1, status=EventStatus.SCHEDULED)
    d.update(kw)
    return Event(**d)


def make_flight(event, **kw) -> Flight:
    d = dict(pilot_id=1, flight_date=event.dtstart.date(), dep_icao="UUEE",
             arr_icao="HECA", block_minutes=345, plan_snapshot=snapshot_of(event),
             source_event_uid=event.uid,
             out_utc=datetime(2026, 9, 21, 9, 52, tzinfo=UTC),
             in_utc=datetime(2026, 9, 21, 15, 37, tzinfo=UTC))
    d.update(kw)
    return Flight(**d)


def test_snapshot_is_frozen_at_creation():
    event = make_event()
    flight = make_flight(event)
    assert flight.plan_snapshot["flight_no"] == "SU400"
    assert flight.plan_snapshot["content_hash"] == "h1"


def test_unchanged_plan_produces_no_divergence():
    event = make_event()
    flight = make_flight(event)
    assert compare_to_plan(flight, event) == []


def test_retroactive_plan_edit_is_detected_not_applied():
    """План поправили задним числом — запись остаётся, пилот уведомляется."""
    event = make_event()
    flight = make_flight(event)

    event.dtstart = datetime(2026, 9, 21, 11, 45, tzinfo=UTC)
    event.content_hash = "h2"

    changes = compare_to_plan(flight, event)
    assert [c.field for c in changes] == ["dtstart"]
    # Сама запись не тронута.
    assert flight.out_utc == datetime(2026, 9, 21, 9, 52, tzinfo=UTC)
    assert flight.block_minutes == 345


def test_vanished_event_is_not_a_divergence():
    """Лента показывает ограниченное окно, старое из неё выпадает."""
    flight = make_flight(make_event())
    assert compare_to_plan(flight, None) == []


def test_cancelled_plan_is_flagged_not_deleted():
    event = make_event(status=EventStatus.CANCELLED)
    flight = make_flight(event)
    assert event_was_cancelled(flight, event) is True


def test_flight_without_plan_is_valid():
    """Перегонка, вылет на запасной вторым плечом — плана не было вовсе."""
    flight = Flight(pilot_id=1, flight_date=datetime(2026, 9, 21, tzinfo=UTC).date(),
                    dep_icao="UUEE", arr_icao="ULLI", block_minutes=95)
    assert compare_to_plan(flight, None) == []
    actual = compare_actual(flight)
    assert actual.diverted is False


def test_diversion_detected():
    """Планировали Каир, сели в другом месте."""
    event = make_event()
    flight = make_flight(event, arr_icao="HESH")
    actual = compare_actual(flight)
    assert actual.diverted is True


def test_iata_and_icao_are_not_a_false_diversion():
    """В ленте CAI, в книжке HECA — это один аэропорт, не уход на запасной.

    Снимок плана берёт коды уже переведёнными, иначе сравнение сработает
    на разнице алфавитов, а не на факте.
    """
    event = make_event()
    flight = make_flight(event, arr_icao="HECA",
                         plan_snapshot=snapshot_of(event, "UUEE", "HECA"))
    assert compare_actual(flight).diverted is False


def test_air_return_detected():
    """Запустились, сломались, вернулись."""
    event = make_event()
    flight = make_flight(event, dep_icao="UUEE", arr_icao="UUEE", block_minutes=55,
                         in_utc=datetime(2026, 9, 21, 10, 47, tzinfo=UTC))
    actual = compare_actual(flight)
    assert actual.air_return is True


def test_delay_and_duration_difference():
    event = make_event()
    flight = make_flight(event)
    actual = compare_actual(flight)
    assert actual.delay_minutes == 7
    assert actual.longer_minutes == 0


def test_iata_is_translated_by_catalogue_not_guessed():
    """ICAO нельзя вывести из IATA: SVO->UUEE, CAI->HECA — общих букв нет."""
    from app.logbook.draft import draft_from_event, resolve_airports

    event = make_event()
    event.kind = EventKind.FLIGHT
    draft = draft_from_event(event)
    assert (draft.dep_icao, draft.arr_icao) == ("SVO", "CAI")

    resolve_airports(draft, {"SVO": "UUEE", "CAI": "HECA"})
    assert (draft.dep_icao, draft.arr_icao) == ("UUEE", "HECA")


def test_unknown_iata_is_left_alone_not_invented():
    from app.logbook.draft import draft_from_event, resolve_airports

    draft = draft_from_event(make_event())
    resolve_airports(draft, {"SVO": "UUEE"})
    assert draft.arr_icao == "CAI"
