"""Поиск по лётной книжке.

Фильтры складываются: указать можно любое сочетание, условия соединяются
через И. Результат всегда возвращается вместе со сводкой по отобранному
множеству — иначе поиск отвечает «какие рейсы», но не отвечает «сколько
я с ним налетал», а это обычно и есть вопрос.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Aircraft,
    Airport,
    CrewRole,
    Employer,
    Flight,
    FlightCrew,
    Function,
    Person,
    PersonAlias,
)
from app.reports.summary import Bucket


@dataclass
class Filters:
    crew: str | None = None          # фамилия или часть имени
    aircraft: str | None = None      # регистрация, любая из двух
    airport: str | None = None       # ICAO, вылет или прилёт
    dep: str | None = None
    arr: str | None = None
    since: date | None = None
    until: date | None = None
    function: Function | None = None
    flight_number: str | None = None
    aircraft_type: str | None = None
    employer: str | None = None
    night_only: bool = False
    limit: int | None = None


@dataclass
class Result:
    flights: list[Flight]
    total: int          # сколько подошло под фильтр до применения limit
    bucket: Bucket
    people: dict[str, int]


def find_people(session: Session, query: str) -> list[Person]:
    """Ищет человека по фамилии, имени или любому из алиасов."""
    pattern = f"%{query}%"
    stmt = (
        select(Person)
        .outerjoin(PersonAlias, PersonAlias.person_id == Person.id)
        .where(
            or_(
                Person.last_name.ilike(pattern),
                Person.first_name.ilike(pattern),
                PersonAlias.alias.ilike(pattern),
            )
        )
        .distinct()
        .order_by(Person.last_name)
    )
    return list(session.scalars(stmt).all())


def find_aircraft(session: Session, query: str) -> list[Aircraft]:
    """Ищет борт по любой из двух регистраций: VQ-BWF и RA-73126 — один самолёт."""
    pattern = f"%{query}%"
    stmt = (
        select(Aircraft)
        .where(
            or_(
                Aircraft.registration.ilike(pattern),
                Aircraft.registration_ra.ilike(pattern),
            )
        )
        .order_by(Aircraft.registration)
    )
    return list(session.scalars(stmt).all())


def search(session: Session, filters: Filters) -> Result:
    stmt = select(Flight)

    if filters.crew:
        people = find_people(session, filters.crew)
        ids = [p.id for p in people]
        if not ids:
            return Result([], 0, Bucket(), {})
        stmt = stmt.where(
            Flight.id.in_(
                select(FlightCrew.flight_id).where(FlightCrew.person_id.in_(ids))
            )
        )

    if filters.aircraft:
        machines = find_aircraft(session, filters.aircraft)
        ids = [a.id for a in machines]
        if not ids:
            return Result([], 0, Bucket(), {})
        stmt = stmt.where(Flight.aircraft_id.in_(ids))

    if filters.aircraft_type:
        pattern = f"%{filters.aircraft_type}%"
        stmt = stmt.where(
            Flight.aircraft_id.in_(
                select(Aircraft.id).where(
                    or_(
                        Aircraft.type_code.ilike(pattern),
                        Aircraft.type_name.ilike(pattern),
                    )
                )
            )
        )

    if filters.airport:
        code = filters.airport.upper()
        stmt = stmt.where(or_(Flight.dep_icao == code, Flight.arr_icao == code))
    if filters.dep:
        stmt = stmt.where(Flight.dep_icao == filters.dep.upper())
    if filters.arr:
        stmt = stmt.where(Flight.arr_icao == filters.arr.upper())

    if filters.since:
        stmt = stmt.where(Flight.flight_date >= filters.since)
    if filters.until:
        stmt = stmt.where(Flight.flight_date <= filters.until)

    if filters.function:
        stmt = stmt.where(Flight.function == filters.function)
    if filters.flight_number:
        stmt = stmt.where(Flight.flight_number.ilike(f"%{filters.flight_number}%"))
    if filters.night_only:
        stmt = stmt.where(Flight.night_minutes > 0)

    if filters.employer:
        stmt = stmt.where(
            Flight.employer_id.in_(
                select(Employer.id).where(Employer.name.ilike(f"%{filters.employer}%"))
            )
        )

    stmt = stmt.order_by(Flight.flight_date.desc(), Flight.out_utc.desc())

    matched = list(session.scalars(stmt).all())

    # Сводка считается по всему отобранному множеству, а не по странице.
    bucket = Bucket()
    partners: dict[str, int] = {}
    for flight in matched:
        bucket.add(flight)
        for slot in flight.crew:
            if slot.person and not slot.person.is_owner:
                partners[slot.person.display] = partners.get(slot.person.display, 0) + 1

    page = matched[: filters.limit] if filters.limit else matched
    return Result(page, len(matched), bucket, partners)


def airports_visited(session: Session) -> dict[str, int]:
    """Сколько раз каждый аэропорт встречался в книжке."""
    counts: dict[str, int] = {}
    for flight in session.scalars(select(Flight)).all():
        for code in (flight.dep_icao, flight.arr_icao):
            if code:
                counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def airport_names(session: Session, codes: list[str]) -> dict[str, str]:
    stmt = select(Airport).where(Airport.icao.in_(codes))
    return {a.icao: (a.municipality or a.name or a.icao) for a in session.scalars(stmt).all()}


ROLE_LABEL = {
    CrewRole.PIC: "КВС",
    CrewRole.SIC: "2П",
    CrewRole.RELIEF: "усиление",
    CrewRole.RELIEF2: "усиление",
    CrewRole.INSTRUCTOR: "инструктор",
    CrewRole.STUDENT: "стажёр",
    CrewRole.OBSERVER: "проверяющий",
    CrewRole.CABIN: "бортпроводник",
}
