"""Доступ к данным лётной книжки и сохранение черновиков."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Aircraft,
    Airport,
    CrewRole,
    Employer,
    Event,
    EventKind,
    Flight,
    FlightCrew,
    FlightSource,
    Function,
    Person,
    PersonAlias,
)
from app.logbook.draft import FlightDraft
from app.logbook.link import snapshot_of
from app.logbook.night import night_minutes

POSITION_TO_ROLE = {
    "КВС": CrewRole.PIC,
    "2П": CrewRole.SIC,
    "СБ": CrewRole.CABIN,
    "БП": CrewRole.CABIN,
    "ИНС": CrewRole.INSTRUCTOR,
    "ПИ": CrewRole.OBSERVER,
}


# --------------------------------------------------------------------------
# Аэропорты
# --------------------------------------------------------------------------


async def iata_to_icao_map(session: AsyncSession, codes: Sequence[str]) -> dict[str, str]:
    wanted = [c.upper() for c in codes if c and len(c) == 3]
    if not wanted:
        return {}
    stmt = select(Airport).where(Airport.iata.in_(wanted))
    return {a.iata: a.icao for a in (await session.execute(stmt)).scalars().all() if a.iata}


async def coords_for(session: AsyncSession, icao: str | None) -> tuple[float, float] | None:
    if not icao:
        return None
    airport = await session.get(Airport, icao.upper())
    return airport.coords if airport else None


# --------------------------------------------------------------------------
# Рейсы, ожидающие записи
# --------------------------------------------------------------------------


async def pending_flights(
    session: AsyncSession, pilot_id: int, now: datetime, back_days: int = 14
) -> list[Event]:
    """Рейсы из расписания, которые уже закончились, но в книжку не попали.

    Только рейсы: медкомиссия, учёба и явка в лётную книжку не идут.
    """
    since = now - timedelta(days=back_days)
    stmt = (
        select(Event)
        .where(
            Event.pilot_id == pilot_id,
            Event.kind == EventKind.FLIGHT,
            Event.dtend <= now,
            Event.dtend >= since,
        )
        .order_by(Event.dtstart.desc())
    )
    events = list((await session.execute(stmt)).scalars().all())
    if not events:
        return []

    logged = set(
        (
            await session.execute(
                select(Flight.source_event_uid).where(
                    Flight.pilot_id == pilot_id,
                    Flight.source_event_uid.in_([e.uid for e in events]),
                )
            )
        )
        .scalars()
        .all()
    )
    return [e for e in events if e.uid not in logged]


async def event_by_uid(session: AsyncSession, pilot_id: int, uid: str) -> Event | None:
    stmt = select(Event).where(Event.pilot_id == pilot_id, Event.uid == uid)
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------
# Справочники пилота
# --------------------------------------------------------------------------


async def owner_person(session: AsyncSession, pilot_id: int) -> Person | None:
    stmt = select(Person).where(Person.pilot_id == pilot_id, Person.is_owner.is_(True))
    return (await session.execute(stmt)).scalars().first()


async def person_for_name(
    session: AsyncSession, pilot_id: int, last: str, first: str | None, middle: str | None
) -> Person:
    """Находит человека по алиасу или заводит нового.

    Слияние похожих фамилий не делается: в справочнике есть Evstafev
    и Evstifeev — разные люди, отличающиеся двумя буквами.
    """
    full = " ".join(p for p in (last, first, middle) if p)
    stmt = (
        select(Person)
        .join(PersonAlias, PersonAlias.person_id == Person.id)
        .where(Person.pilot_id == pilot_id, PersonAlias.alias == full)
    )
    found = (await session.execute(stmt)).scalars().first()
    if found is not None:
        return found

    stmt = select(Person).where(
        Person.pilot_id == pilot_id,
        Person.last_name == last,
        Person.first_name == first,
    )
    found = (await session.execute(stmt)).scalars().first()
    if found is not None:
        found.aliases.append(PersonAlias(alias=full))
        await session.flush()
        return found

    person = Person(pilot_id=pilot_id, last_name=last, first_name=first, middle_name=middle)
    person.aliases.append(PersonAlias(alias=full))
    session.add(person)
    await session.flush()
    return person


async def aircraft_by_id(session: AsyncSession, aircraft_id: int) -> Aircraft | None:
    return await session.get(Aircraft, aircraft_id)


async def search_aircraft(
    session: AsyncSession, fragment: str, limit: int = 8
) -> list[Aircraft]:
    cleaned = fragment.replace("-", "").replace(" ", "").upper()
    if not cleaned:
        return []
    plain = func.upper(func.replace(Aircraft.registration, "-", ""))
    plain_ra = func.upper(func.replace(Aircraft.registration_ra, "-", ""))
    stmt = (
        select(Aircraft)
        .where(or_(plain.like(f"%{cleaned}%"), plain_ra.like(f"%{cleaned}%")))
        .order_by(Aircraft.registration)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def employer_for(session: AsyncSession, pilot_id: int, day: date) -> Employer | None:
    stmt = select(Employer).where(Employer.pilot_id == pilot_id)
    for employer in (await session.execute(stmt)).scalars().all():
        if employer.covers(day):
            return employer
    return None


# --------------------------------------------------------------------------
# Сохранение
# --------------------------------------------------------------------------


async def save_draft(
    session: AsyncSession,
    pilot_id: int,
    draft: FlightDraft,
    function: Function,
    aircraft_id: int | None,
    event: Event | None = None,
) -> Flight:
    """Записывает черновик в книжку.

    Ночное время считается автоматически: формула проверена на истории
    и с 2022 года расходится с LogTen меньше чем на минуту. Если координат
    аэропорта нет, остаётся ноль и пометка, что расчёт не выполнялся.
    """
    day = draft.flight_date.date()
    flight = Flight(
        pilot_id=pilot_id,
        flight_date=day,
        out_utc=draft.actual_out,
        in_utc=draft.actual_in,
        dep_icao=draft.dep_icao,
        arr_icao=draft.arr_icao,
        aircraft_id=aircraft_id,
        flight_number=draft.flight_number,
        block_minutes=draft.block_minutes or 0,
        function=function,
        function_source="должность в задании" if function is not Function.UNVERIFIED else None,
        remarks=draft.remarks,
        source=FlightSource.CALENDAR if event is not None else FlightSource.MANUAL,
        source_event_uid=draft.event_uid if event is not None else None,
    )

    employer = await employer_for(session, pilot_id, day)
    flight.employer_id = employer.id if employer else None

    if event is not None:
        flight.plan_snapshot = snapshot_of(event, draft.dep_icao, draft.arr_icao)

    start, end = draft.duty_window()
    flight.on_duty_utc = start
    flight.off_duty_utc = end
    flight.duty_minutes = draft.duty_minutes
    flight.duty_computed = True

    dep = await coords_for(session, draft.dep_icao)
    arr = await coords_for(session, draft.arr_icao)
    if draft.actual_out and draft.actual_in:
        computed = night_minutes(draft.actual_out, draft.actual_in, dep, arr)
        if computed is not None:
            flight.night_minutes = computed
            flight.night_computed = True

    flight.day_landings = 0 if flight.night_minutes else 1
    flight.night_landings = 1 if flight.night_minutes else 0

    session.add(flight)
    await session.flush()

    for member in draft.crew:
        person = await person_for_name(
            session, pilot_id, member.last_name, member.first_name, member.middle_name
        )
        if member.is_owner:
            person.is_owner = True
        role = POSITION_TO_ROLE.get(member.position)
        if role is None:
            continue
        session.add(FlightCrew(flight_id=flight.id, person_id=person.id, role=role))

    await session.flush()
    return flight


async def month_totals(session: AsyncSession, pilot_id: int, start: date, end: date) -> dict:
    stmt = select(Flight).where(
        Flight.pilot_id == pilot_id, Flight.flight_date >= start, Flight.flight_date < end
    )
    flights = list((await session.execute(stmt)).scalars().all())
    return {
        "flights": len(flights),
        "block": sum(f.block_minutes for f in flights),
        "pic": sum(f.easa_pic_minutes for f in flights),
        "copilot": sum(f.easa_copilot_minutes for f in flights),
        "night": sum(f.night_minutes for f in flights),
        "duty": sum(f.duty_minutes or 0 for f in flights),
    }


async def recent_flights(
    session: AsyncSession, pilot_id: int, limit: int = 10
) -> list[Flight]:
    stmt = (
        select(Flight)
        .where(Flight.pilot_id == pilot_id)
        .order_by(Flight.flight_date.desc(), Flight.out_utc.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
