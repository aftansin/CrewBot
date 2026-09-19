"""Разбор отдельных значений из выгрузки LogTen.

Все четыре формата-ловушки, найденные на реальном файле, обрабатываются тут:

* расстояние приходит как ``1 175,3`` — неразрывный пробел между тысячами
  и запятая вместо точки, обычный ``float()`` на этом падает;
* ``aircraft_year`` содержит не год, а метку времени с зоной;
* время прилёта бывает "раньше" вылета — это переход через полночь;
* часть полей пустая, и пустота значима: её нельзя подменять нулём.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

NBSP = "\u00a0"
NNBSP = "\u202f"

_DURATION_RE = re.compile(r"^(\d+):([0-5]\d)$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ValueError_(ValueError):
    """Значение не разобрано. Импортёр обязан сообщить о таком, а не молчать."""


def text(raw: str | None) -> str | None:
    """Пустая строка превращается в None: пусто и ноль — разные вещи."""
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def duration_minutes(raw: str | None) -> int | None:
    """``4:50`` -> 290. Часы могут быть больше 24."""
    value = text(raw)
    if value is None:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError_(f"не похоже на длительность: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def clock(raw: str | None) -> time | None:
    value = text(raw)
    if value is None:
        return None
    match = _TIME_RE.match(value)
    if not match:
        raise ValueError_(f"не похоже на время суток: {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def flight_date(raw: str | None) -> date | None:
    value = text(raw)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError_(f"не похоже на дату: {value!r}") from exc


def combine_utc(day: date, moment: time) -> datetime:
    return datetime(day.year, day.month, day.day, moment.hour, moment.minute, tzinfo=UTC)


def departure_arrival(
    day: date, dep: time | None, arr: time | None
) -> tuple[datetime | None, datetime | None]:
    """Собирает отметки в UTC, разворачивая переход через полночь.

    В выгрузке 386 рейсов, где прилёт по часам "раньше" вылета. Вычитание
    в лоб дало бы отрицательную длительность.
    """
    if dep is None or arr is None:
        return (combine_utc(day, dep) if dep else None, combine_utc(day, arr) if arr else None)
    out = combine_utc(day, dep)
    inn = combine_utc(day, arr)
    if inn <= out:
        inn += timedelta(days=1)
    return out, inn


def decimal(raw: str | None) -> float | None:
    """``1 175,3`` -> 1175.3. Терпит неразрывные пробелы и запятую."""
    value = text(raw)
    if value is None:
        return None
    cleaned = value.replace(NBSP, "").replace(NNBSP, "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError_(f"не похоже на число: {value!r}") from exc


def integer(raw: str | None) -> int | None:
    value = text(raw)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError_(f"не похоже на целое: {value!r}") from exc


def year_from_timestamp(raw: str | None) -> int | None:
    """``2015-11-22 23:17:22 +0000`` -> 2015. Принимает и простой год."""
    value = text(raw)
    if value is None:
        return None
    if re.fullmatch(r"\d{4}", value):
        return int(value)
    match = re.match(r"^(\d{4})-\d{2}-\d{2}", value)
    if match:
        return int(match.group(1))
    raise ValueError_(f"не похоже на год: {value!r}")


def boolean(raw: str | None) -> bool:
    value = text(raw)
    return value is not None and value.lower() in {"1", "true", "yes", "y"}


def fmt_minutes(total: int) -> str:
    return f"{total // 60}:{total % 60:02d}"
