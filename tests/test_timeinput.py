"""Тесты разбора и проверки введённых времён."""
from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from app.logbook.timeinput import (
    Severity,
    build_datetimes,
    parse_and_validate,
    parse_times,
    validate,
)

MSK = ZoneInfo("Europe/Moscow")
DATE = datetime(2026, 9, 21, 9, 45, tzinfo=UTC)
SCHED_OUT = datetime(2026, 9, 21, 9, 45, tzinfo=UTC)
SCHED_IN = datetime(2026, 9, 21, 15, 30, tzinfo=UTC)


def test_accepts_common_formats():
    for text in ("0952 1537", "09:52 15:37", "0952-1537", "0952/1537", "  0952   1537 "):
        parsed = parse_times(text)
        assert parsed.out == time(9, 52), text
        assert parsed.inn == time(15, 37), text


def test_rejects_wrong_count():
    for text in ("0952", "0952 1537 1600", ""):
        assert parse_times(text).errors


def test_rejects_impossible_clock():
    for text in ("2561 1537", "0952 2599", "abcd 1537"):
        assert parse_times(text).errors


def test_midnight_crossing_is_unwrapped():
    out_dt, in_dt = build_datetimes(DATE, time(22, 30), time(2, 15))
    assert in_dt.day == 22
    assert int((in_dt - out_dt).total_seconds() // 60) == 225


def test_normal_flight_passes_clean():
    out_dt, in_dt, issues = parse_and_validate("0952 1537", DATE, SCHED_OUT, SCHED_IN)
    assert out_dt is not None
    assert not issues


def test_swapped_times_are_rejected():
    """1537 9552 наоборот дало бы почти сутки — это ошибка, не переход через полночь."""
    _out, _in, issues = parse_and_validate("1537 0952", DATE, SCHED_OUT, SCHED_IN)
    assert any(i.severity == Severity.ERROR for i in issues)


def test_moscow_time_instead_of_utc_is_caught():
    """Главная ловушка: план в московском, книжка в UTC. Разница ровно три часа."""
    _out, _in, issues = parse_and_validate("1245 1830", DATE, SCHED_OUT, SCHED_IN)
    messages = " ".join(i.message + (i.hint or "") for i in issues)
    assert "три часа" in messages
    assert "UTC" in messages


def test_ordinary_delay_is_not_flagged_as_timezone():
    """Полтора часа задержки — обычное дело, про UTC говорить не надо."""
    _out, _in, issues = parse_and_validate("1115 1700", DATE, SCHED_OUT, SCHED_IN)
    assert "три часа" not in " ".join(i.message for i in issues)


def test_wrong_date_is_an_error():
    _out, _in, issues = parse_and_validate("2330 0510", DATE, SCHED_OUT, SCHED_IN)
    assert any(i.severity == Severity.ERROR for i in issues)


def test_air_return_warns_but_saves():
    out_dt, _in, issues = parse_and_validate("0952 1035", DATE, SCHED_OUT, SCHED_IN)
    assert out_dt is not None
    assert issues
    assert all(i.severity == Severity.WARNING for i in issues)


def test_much_longer_than_plan_warns():
    _out, _in, issues = parse_and_validate("0952 1830", DATE, SCHED_OUT, SCHED_IN)
    text = " ".join(i.message for i in issues)
    assert "дольше" in text


def test_validation_without_plan_still_checks_sanity():
    out_dt = datetime(2026, 9, 21, 9, 52, tzinfo=UTC)
    in_dt = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    issues = validate(out_dt, in_dt)
    assert any(i.severity == Severity.ERROR for i in issues)


def test_errors_prevent_saving():
    out_dt, in_dt, issues = parse_and_validate("9999 1537", DATE)
    assert out_dt is None and in_dt is None
    assert issues
