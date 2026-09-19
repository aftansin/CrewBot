"""Тесты моста «расписание -> черновик записи» и расчёта ночного времени."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import Event, EventKind
from app.logbook.draft import (
    DUTY_AFTER_ARRIVAL,
    DUTY_BEFORE_DEPARTURE,
    draft_from_event,
    owner_position,
    suggested_function,
)
from app.logbook.night import night_minutes, sun_elevation

UTC = UTC

# Координаты из справочника аэропортов.
ANTALYA = (36.8987, 30.8005)
SHEREMETYEVO = (55.9726, 37.4146)
IRKUTSK = (52.2680, 104.3889)


def make_event(**kwargs) -> Event:
    defaults = dict(
        uid="6217323",
        kind=EventKind.FLIGHT,
        summary="SU1562 Moscow (SVO | Sheremetyevo (B)) \u2192 Irkutsk (IKT | Irkutsk)",
        description=(
            "[SU1562, B-737-800H] Moscow \u2192 Irkutsk\n\n"
            "Pervyi Ivan Ivanovich (\u041a\u0412\u0421);\n"
            "Vtoroi Petr Petrovich (2\u041f)"
        ),
        dtstart=datetime(2026, 9, 15, 1, 30, tzinfo=UTC),
        dtend=datetime(2026, 9, 15, 7, 10, tzinfo=UTC),
        flight_no="SU1562",
        dep_code="SVO",
        arr_code="IKT",
        aircraft="B-737-800H",
        content_hash="x",
        pilot_id=1,
    )
    defaults.update(kwargs)
    return Event(**defaults)


# --------------------------------------------------------------------------
# Мост
# --------------------------------------------------------------------------


def test_only_flights_become_drafts():
    """Медкомиссия, учёба и явка в лётную книжку не попадают."""
    for kind in (EventKind.MEDICAL, EventKind.TRAINING, EventKind.REPORTING, EventKind.REST):
        assert draft_from_event(make_event(kind=kind)) is None
    assert draft_from_event(make_event()) is not None


def test_draft_prefills_what_schedule_knows():
    draft = draft_from_event(make_event())
    assert draft.flight_number == "SU1562"
    assert draft.aircraft_type == "B-737-800H"
    assert draft.dep_icao == "SVO"
    assert draft.arr_icao == "IKT"
    assert len(draft.crew) == 2


def test_draft_does_not_invent_actual_times():
    """Плановое время не должно попадать в налёт под видом фактического."""
    draft = draft_from_event(make_event())
    assert draft.scheduled_out is not None
    assert draft.actual_out is None
    assert draft.actual_in is None
    assert draft.block_minutes is None
    assert not draft.is_complete


def test_missing_lists_what_pilot_must_enter():
    draft = draft_from_event(make_event())
    gaps = draft.missing
    assert "время запуска" in gaps
    assert "время выключения" in gaps
    # Регистрации в ленте нет, только тип борта.
    assert "регистрация борта" in gaps


def test_block_time_from_actual_times():
    draft = draft_from_event(make_event())
    draft.actual_out = datetime(2026, 9, 15, 1, 35, tzinfo=UTC)
    draft.actual_in = datetime(2026, 9, 15, 7, 25, tzinfo=UTC)
    assert draft.block_minutes == 350
    assert draft.is_complete


def test_duty_window_uses_scheduled_departure_and_actual_arrival():
    """Явка считается от планового отправления, смена кончается по факту."""
    draft = draft_from_event(make_event())
    draft.actual_in = datetime(2026, 9, 15, 7, 25, tzinfo=UTC)

    start, end = draft.duty_window()
    assert start == draft.scheduled_out - DUTY_BEFORE_DEPARTURE
    assert end == draft.actual_in + DUTY_AFTER_ARRIVAL
    # 01:30 минус час = 00:30, 07:25 плюс полчаса = 07:55 -> 7 ч 25 мин
    assert draft.duty_minutes == 445


def test_duty_unknown_until_arrival_entered():
    draft = draft_from_event(make_event())
    assert draft.duty_minutes is None


def test_owner_position_and_function():
    draft = draft_from_event(make_event(), owner_last_name="Pervyi")
    assert owner_position(draft) == "\u041a\u0412\u0421"
    assert suggested_function(draft) == "pic"

    draft = draft_from_event(make_event(), owner_last_name="Vtoroi")
    assert suggested_function(draft) == "copilot"


def test_function_not_suggested_when_owner_absent():
    draft = draft_from_event(make_event(), owner_last_name="Tretiy")
    assert suggested_function(draft) is None


# --------------------------------------------------------------------------
# Ночное время
# --------------------------------------------------------------------------


def test_sun_elevation_is_negative_at_local_midnight():
    midnight = datetime(2026, 6, 21, 21, 0, tzinfo=UTC)  # полночь в Москве
    assert sun_elevation(midnight, *SHEREMETYEVO) < 0


def test_night_matches_real_logbook_entry():
    """Проверка на реальном рейсе: 18.09.2026 LTAI-UUEE, в книжке 3:43.

    Допуск пять минут: это точность, подтверждённая на выборке из
    367 рейсов, где медиана расхождения составила одну минуту.
    """
    out = datetime(2026, 9, 18, 15, 18, tzinfo=UTC)
    arrive = datetime(2026, 9, 18, 20, 8, tzinfo=UTC)
    computed = night_minutes(out, arrive, ANTALYA, SHEREMETYEVO)
    assert computed is not None
    assert abs(computed - 223) <= 5


def test_night_returns_none_without_coordinates():
    """Ноль означал бы «рейс был дневным», а это не установлено."""
    out = datetime(2026, 9, 15, 1, 30, tzinfo=UTC)
    arrive = datetime(2026, 9, 15, 7, 10, tzinfo=UTC)
    assert night_minutes(out, arrive, None, IRKUTSK) is None
    assert night_minutes(out, arrive, SHEREMETYEVO, None) is None


def test_night_never_exceeds_block_time():
    out = datetime(2026, 12, 21, 22, 0, tzinfo=UTC)
    arrive = out + timedelta(hours=5)
    computed = night_minutes(out, arrive, SHEREMETYEVO, IRKUTSK)
    assert 0 <= computed <= 300


def test_zero_length_flight_is_zero_night():
    moment = datetime(2026, 9, 15, 1, 30, tzinfo=UTC)
    assert night_minutes(moment, moment, SHEREMETYEVO, IRKUTSK) == 0
