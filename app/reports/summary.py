"""Сводки по налёту.

Главное назначение на этом этапе — сверка с LogTen. Поэтому сводка
показывает те же величины, что видны в приложении: общий налёт за месяц,
функциональные колонки, ночное время, взлёты и посадки.

Всё считается из фактов в базе, ничего не хранится отдельно: пересчёт
после правки рейса происходит сам.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employer, Flight, Function

# Функции, засчитываемые в колонку PIC по EASA.
PIC_FUNCTIONS = {Function.PIC, Function.PICUS, Function.FI, Function.FE}
COPILOT_FUNCTIONS = {Function.COPILOT, Function.CRUISE_RELIEF}


@dataclass
class Bucket:
    flights: int = 0
    block: int = 0
    pic: int = 0
    copilot: int = 0
    dual: int = 0
    unverified: int = 0
    night: int = 0
    day_landings: int = 0
    night_landings: int = 0
    types: set[str] = field(default_factory=set)

    def add(self, flight: Flight) -> None:
        self.flights += 1
        self.block += flight.block_minutes
        if flight.function in PIC_FUNCTIONS:
            self.pic += flight.block_minutes
        elif flight.function in COPILOT_FUNCTIONS:
            self.copilot += flight.block_minutes
        elif flight.function is Function.DUAL:
            self.dual += flight.block_minutes
        else:
            self.unverified += flight.block_minutes
        self.night += flight.night_minutes
        self.day_landings += flight.day_landings
        self.night_landings += flight.night_landings
        if flight.aircraft and flight.aircraft.type_code:
            self.types.add(flight.aircraft.type_code)


def _load(session: Session, start: date | None, end: date | None) -> list[Flight]:
    stmt = select(Flight).order_by(Flight.flight_date)
    if start:
        stmt = stmt.where(Flight.flight_date >= start)
    if end:
        stmt = stmt.where(Flight.flight_date <= end)
    return list(session.scalars(stmt).all())


def by_month(session: Session, start: date | None = None, end: date | None = None):
    result: dict[tuple[int, int], Bucket] = defaultdict(Bucket)
    for flight in _load(session, start, end):
        result[(flight.flight_date.year, flight.flight_date.month)].add(flight)
    return dict(sorted(result.items()))


def by_year(session: Session, start: date | None = None, end: date | None = None):
    result: dict[int, Bucket] = defaultdict(Bucket)
    for flight in _load(session, start, end):
        result[flight.flight_date.year].add(flight)
    return dict(sorted(result.items()))


def by_employer(session: Session):
    employers = {e.id: e for e in session.scalars(select(Employer)).all()}
    result: dict[str, Bucket] = defaultdict(Bucket)
    for flight in _load(session, None, None):
        employer = employers.get(flight.employer_id)
        label = f"{employer.name} ({employer.started_on:%m.%Y})" if employer else "вне периодов"
        result[label].add(flight)
    return result


def rolling(session: Session, today: date, windows=(28, 90, 365)) -> dict[int, Bucket]:
    """Налёт за скользящие окна — то, чем проверяют ограничения по времени."""
    result: dict[int, Bucket] = {}
    for days in windows:
        bucket = Bucket()
        for flight in _load(session, today - timedelta(days=days - 1), today):
            bucket.add(flight)
        result[days] = bucket
    return result


def landings_in_last_days(session: Session, today: date, days: int = 90) -> tuple[int, int]:
    """Взлёты и посадки за период. Для проверки собственной актуальности."""
    day = 0
    night = 0
    for flight in _load(session, today - timedelta(days=days - 1), today):
        day += flight.day_landings
        night += flight.night_landings
    return day, night
