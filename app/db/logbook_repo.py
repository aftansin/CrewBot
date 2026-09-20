"""Доступ к данным лётной книжки и сохранение черновиков."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
    EventStatus,
    Flight,
    FlightCrew,
    FlightRevision,
    FlightSource,
    Function,
    Person,
    PersonAlias,
    Pilot,
)
from app.logbook.draft import FlightDraft
from app.logbook.link import snapshot_of
from app.logbook.night import night_minutes
from app.logbook.translit import person_key, same_script, to_display_latin


def normalize_flight_number(value: str | None) -> str:
    """Только цифры номера рейса.

    В выгрузке LogTen номера записаны без кода перевозчика ("1562"),
    а из ленты расписания приходит "SU1562". Сравнение как есть
    не совпадало никогда — из-за этого бот предлагал записать рейсы,
    которые уже лежали в книжке, и получались дубли.
    """
    return "".join(c for c in (value or "") if c.isdigit())


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

    Совпадение с уже записанным ищется двумя способами, и второй важнее
    первого. По ``source_event_uid`` находятся рейсы, заведённые через
    бота. Но у рейсов, перенесённых из LogTen, эта ссылка пустая — они
    пришли из выгрузки, а не из календаря. Если проверять только её,
    бот предложит записать заново всё, что уже есть в книжке, и создаст
    дубли. Поэтому дополнительно сверяется дата и маршрут.
    """
    since = now - timedelta(days=back_days)
    stmt = (
        select(Event)
        .where(
            Event.pilot_id == pilot_id,
            Event.kind == EventKind.FLIGHT,
            Event.dtend <= now,
            Event.dtend >= since,
            # Отменённый рейс записывать не предлагаем.
            Event.status == EventStatus.SCHEDULED,
        )
        .order_by(Event.dtstart.desc())
    )
    events = list((await session.execute(stmt)).scalars().all())
    if not events:
        return []

    # План правится задним числом: рейс мог исчезнуть из расписания уже
    # после даты вылета. Отмену прошедших событий diff намеренно не ставит
    # (лента показывает ограниченное окно, старое из неё выпадает сама),
    # поэтому здесь смотрим иначе: попало ли событие в последнюю выгрузку.
    # Не попало — значит из плана его убрали, и рейс не выполнялся.
    pilot = await session.get(Pilot, pilot_id)
    if pilot is not None and pilot.last_sync_at is not None:
        threshold = pilot.last_sync_at - timedelta(minutes=5)
        events = [
            e for e in events
            if e.last_seen_at is not None and e.last_seen_at >= threshold
        ]
        if not events:
            return []

    days = {e.dtstart.date() for e in events}
    stmt = select(Flight).where(
        Flight.pilot_id == pilot_id,
        Flight.flight_date >= min(days) - timedelta(days=1),
        Flight.flight_date <= max(days) + timedelta(days=1),
    )
    existing = list((await session.execute(stmt)).scalars().all())

    by_uid = {f.source_event_uid for f in existing if f.source_event_uid}
    # Коды в ленте IATA, в книжке ICAO — сравниваем в одном алфавите.
    codes = {c for e in events for c in (e.dep_code, e.arr_code) if c}
    mapping = await iata_to_icao_map(session, list(codes))

    def route_key(day, dep, arr, number):
        dep = mapping.get((dep or "").upper(), dep)
        arr = mapping.get((arr or "").upper(), arr)
        return (day, (dep or "").upper(), (arr or "").upper(),
                normalize_flight_number(number))

    # К ключу добавляется время вылета: за день по одному маршруту
    # бывает две ротации, и одна не должна прятать другую.
    by_route: dict[tuple, list[datetime]] = {}
    for f in existing:
        key = route_key(f.flight_date, f.dep_icao, f.arr_icao, f.flight_number)
        by_route.setdefault(key, []).append(f.out_utc)

    result = []
    for event in events:
        if event.uid in by_uid:
            continue
        key = route_key(
            event.dtstart.date(), event.dep_code, event.arr_code, event.flight_no
        )
        times = by_route.get(key)
        if times is not None and any(
            t is None or abs((t - event.dtstart).total_seconds()) < 6 * 3600
            for t in times
        ):
            continue
        result.append(event)
    return result


async def find_duplicates(session: AsyncSession, pilot_id: int) -> list[tuple]:
    """Рейсы, задублированные по дате, маршруту и номеру.

    Нужны, чтобы вычистить последствия ошибки в определении незаписанных
    рейсов. Возвращает пары (оставить, удалить).
    """
    stmt = select(Flight).where(Flight.pilot_id == pilot_id).order_by(Flight.id)
    flights = list((await session.execute(stmt)).scalars().all())

    seen: dict[tuple, Flight] = {}
    pairs: list[tuple] = []
    for flight in flights:
        key = (
            flight.flight_date,
            (flight.dep_icao or "").upper(),
            (flight.arr_icao or "").upper(),
            normalize_flight_number(flight.flight_number),
        )
        if key in seen:
            pairs.append((seen[key], flight))
        else:
            seen[key] = flight
    return pairs


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

    Лента отдаёт ФИО кириллицей, а книжка из LogTen хранит латиницу —
    это один человек, но буквального совпадения нет. Поэтому сравнение
    идёт по огрублённому ключу (см. translit), который сводит оба
    алфавита и разные системы транслитерации к одному виду.

    Слияние по одной фамилии не делается: Evstafev и Evstifeev —
    разные люди, и ключ у них тоже разный.
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

    # Точное совпадение фамилии и имени.
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

    # Совпадение через транслитерацию: перебираем однофамильцев по ключу.
    key = person_key(last, first)
    stmt = select(Person).where(Person.pilot_id == pilot_id)
    for candidate in (await session.execute(stmt)).scalars().all():
        if person_key(candidate.last_name, candidate.first_name) == key:
            candidate.aliases.append(PersonAlias(alias=full))
            if (
                not candidate.middle_name
                and middle
                and same_script(candidate.last_name, middle)
            ):
                candidate.middle_name = middle
            await session.flush()
            return candidate

    # Основное написание — латиница: отчёты уходят в зарубежные
    # авиакомпании, там кириллицу не прочитают. Написание из ленты
    # сохраняется в алиасах, поиск работает по обоим.
    person = Person(
        pilot_id=pilot_id,
        last_name=to_display_latin(last),
        first_name=to_display_latin(first),
        middle_name=to_display_latin(middle),
    )
    person.aliases.append(PersonAlias(alias=full))
    latin_full = " ".join(
        p for p in (person.last_name, person.first_name, person.middle_name) if p
    )
    if latin_full != full:
        person.aliases.append(PersonAlias(alias=latin_full))
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


async def count_flights(session: AsyncSession, pilot_id: int) -> int:
    stmt = select(func.count(Flight.id)).where(Flight.pilot_id == pilot_id)
    return int((await session.execute(stmt)).scalar_one())


async def recent_flights(
    session: AsyncSession, pilot_id: int, limit: int = 10, offset: int = 0
) -> list[Flight]:
    stmt = (
        select(Flight)
        .where(Flight.pilot_id == pilot_id)
        .order_by(Flight.flight_date.desc(), Flight.out_utc.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def dismiss_event(session: AsyncSession, pilot_id: int, uid: str) -> bool:
    """Помечает событие как не выполнявшееся.

    Нужно, когда план поменяли задним числом и рейса не было: бот иначе
    будет предлагать записать его бесконечно. Само событие не удаляется —
    если оно вернётся в ленту, синхронизация восстановит статус.
    """
    event = await event_by_uid(session, pilot_id, uid)
    if event is None:
        return False
    event.status = EventStatus.CANCELLED
    event.cancelled_at = datetime.now(tz=event.dtstart.tzinfo)
    await session.flush()
    return True


# --------------------------------------------------------------------------
# Правка записей
# --------------------------------------------------------------------------

EDITABLE_LABELS = {
    "times": "времена",
    "aircraft": "борт",
    "function": "функция",
    "route": "маршрут",
    "remarks": "заметка",
}


async def get_flight(session: AsyncSession, pilot_id: int, flight_id: int) -> Flight | None:
    stmt = select(Flight).where(Flight.id == flight_id, Flight.pilot_id == pilot_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def revisions_for(
    session: AsyncSession, flight_id: int, limit: int = 20
) -> list[FlightRevision]:
    stmt = (
        select(FlightRevision)
        .where(FlightRevision.flight_id == flight_id)
        .order_by(FlightRevision.changed_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def _log(session: AsyncSession, flight: Flight, field: str, old, new) -> None:
    """Пишет правку в историю. Значения приводим к строке как есть."""
    if old == new:
        return
    session.add(
        FlightRevision(
            flight_id=flight.id,
            field=field,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
        )
    )


async def _recompute_derived(session: AsyncSession, flight: Flight) -> None:
    """Пересчитывает то, что зависит от времён и маршрута.

    Ночное время и блок-тайм нельзя оставлять от прежних значений: после
    правки времён они стали бы неверными, а расхождение всплыло бы только
    при сверке годовых сумм.
    """
    if flight.out_utc and flight.in_utc:
        flight.block_minutes = max(
            0, int((flight.in_utc - flight.out_utc).total_seconds() // 60)
        )

    dep = await coords_for(session, flight.dep_icao)
    arr = await coords_for(session, flight.arr_icao)
    if flight.out_utc and flight.in_utc:
        computed = night_minutes(flight.out_utc, flight.in_utc, dep, arr)
        if computed is None:
            # Координат нет: прежнее значение оставляем, но помечаем,
            # что оно не рассчитано, а перенесено или введено руками.
            flight.night_computed = False
        else:
            flight.night_minutes = computed
            flight.night_computed = True
            flight.day_landings = 0 if computed else 1
            flight.night_landings = 1 if computed else 0

    if flight.off_duty_utc and flight.in_utc:
        # Конец смены привязан к фактическому выключению.
        flight.off_duty_utc = flight.in_utc + timedelta(minutes=30)
    if flight.on_duty_utc and flight.off_duty_utc:
        flight.duty_minutes = max(
            0, int((flight.off_duty_utc - flight.on_duty_utc).total_seconds() // 60)
        )


async def edit_times(
    session: AsyncSession, flight: Flight, out_utc: datetime, in_utc: datetime
) -> Flight:
    _log(session, flight, "out_utc", flight.out_utc, out_utc)
    _log(session, flight, "in_utc", flight.in_utc, in_utc)
    old_block = flight.block_minutes
    flight.out_utc = out_utc
    flight.in_utc = in_utc
    await _recompute_derived(session, flight)
    _log(session, flight, "block_minutes", old_block, flight.block_minutes)
    await session.flush()
    return flight


async def edit_aircraft(
    session: AsyncSession, flight: Flight, aircraft_id: int | None
) -> Flight:
    old = flight.aircraft.display if flight.aircraft else None
    new_aircraft = await aircraft_by_id(session, aircraft_id) if aircraft_id else None
    _log(session, flight, "aircraft", old, new_aircraft.display if new_aircraft else None)
    flight.aircraft_id = aircraft_id
    await session.flush()
    return flight


async def edit_function(session: AsyncSession, flight: Flight, function: Function) -> Flight:
    _log(session, flight, "function", flight.function.value, function.value)
    flight.function = function
    flight.function_source = "изменено вручную"
    await session.flush()
    return flight


async def edit_route(
    session: AsyncSession, flight: Flight, dep_icao: str, arr_icao: str
) -> Flight:
    _log(session, flight, "dep_icao", flight.dep_icao, dep_icao)
    _log(session, flight, "arr_icao", flight.arr_icao, arr_icao)
    flight.dep_icao = dep_icao
    flight.arr_icao = arr_icao
    await _recompute_derived(session, flight)
    await session.flush()
    return flight


async def edit_remarks(session: AsyncSession, flight: Flight, remarks: str | None) -> Flight:
    _log(session, flight, "remarks", flight.remarks, remarks)
    flight.remarks = remarks
    await session.flush()
    return flight


async def known_airport(session: AsyncSession, icao: str) -> Airport | None:
    return await session.get(Airport, icao.upper())


# --------------------------------------------------------------------------
# Поиск по книжке
# --------------------------------------------------------------------------


@dataclass
class SearchResult:
    flights: list[Flight]
    total: int
    block: int
    night: int
    pic: int
    copilot: int
    partners: dict[str, int]
    subject: str


async def search_people(session: AsyncSession, pilot_id: int, query: str) -> list[Person]:
    """Ищет человека по фамилии, имени или любому написанию из алиасов.

    Поиск идёт и по огрублённому ключу, поэтому "Цибульников",
    "Tsibulnikov" и "Tsybulnikov" находят одного и того же человека.
    """
    pattern = f"%{query.strip()}%"
    stmt = (
        select(Person)
        .outerjoin(PersonAlias, PersonAlias.person_id == Person.id)
        .where(
            Person.pilot_id == pilot_id,
            or_(
                Person.last_name.ilike(pattern),
                Person.first_name.ilike(pattern),
                PersonAlias.alias.ilike(pattern),
            ),
        )
        .distinct()
    )
    found = list((await session.execute(stmt)).scalars().all())
    if found:
        return found

    key = person_key(query.strip())
    stmt = select(Person).where(Person.pilot_id == pilot_id)
    return [
        p
        for p in (await session.execute(stmt)).scalars().all()
        if person_key(p.last_name) == key
    ]


async def _summarize(
    session: AsyncSession, flights: list[Flight], subject: str, limit: int
) -> SearchResult:
    partners: dict[str, int] = {}
    for flight in flights:
        for slot in flight.crew:
            if slot.person and not slot.person.is_owner:
                name = slot.person.display
                partners[name] = partners.get(name, 0) + 1

    return SearchResult(
        flights=flights[:limit],
        total=len(flights),
        block=sum(f.block_minutes for f in flights),
        night=sum(f.night_minutes for f in flights),
        pic=sum(f.easa_pic_minutes for f in flights),
        copilot=sum(f.easa_copilot_minutes for f in flights),
        partners=partners,
        subject=subject,
    )


async def search_by_crew(
    session: AsyncSession, pilot_id: int, query: str, limit: int = 10
) -> SearchResult | None:
    people = await search_people(session, pilot_id, query)
    people = [p for p in people if not p.is_owner]
    if not people:
        return None

    ids = [p.id for p in people]
    stmt = (
        select(Flight)
        .where(
            Flight.pilot_id == pilot_id,
            Flight.id.in_(select(FlightCrew.flight_id).where(FlightCrew.person_id.in_(ids))),
        )
        .order_by(Flight.flight_date.desc())
    )
    flights = list((await session.execute(stmt)).scalars().all())
    subject = ", ".join(p.display for p in people[:3])
    return await _summarize(session, flights, subject, limit)


async def search_by_aircraft(
    session: AsyncSession, pilot_id: int, query: str, limit: int = 10
) -> SearchResult | None:
    machines = await search_aircraft(session, query, limit=20)
    if not machines:
        return None
    ids = [a.id for a in machines]
    stmt = (
        select(Flight)
        .where(Flight.pilot_id == pilot_id, Flight.aircraft_id.in_(ids))
        .order_by(Flight.flight_date.desc())
    )
    flights = list((await session.execute(stmt)).scalars().all())
    subject = ", ".join(a.display for a in machines[:3])
    return await _summarize(session, flights, subject, limit)


async def search_by_airport(
    session: AsyncSession, pilot_id: int, query: str, limit: int = 10
) -> SearchResult | None:
    code = query.strip().upper()
    if len(code) == 3:
        mapping = await iata_to_icao_map(session, [code])
        code = mapping.get(code, code)
    if len(code) != 4:
        return None
    stmt = (
        select(Flight)
        .where(
            Flight.pilot_id == pilot_id,
            or_(Flight.dep_icao == code, Flight.arr_icao == code),
        )
        .order_by(Flight.flight_date.desc())
    )
    flights = list((await session.execute(stmt)).scalars().all())
    if not flights:
        return None

    airport = await known_airport(session, code)
    subject = f"{code}" + (f" \u2014 {airport.municipality}" if airport and airport.municipality else "")
    return await _summarize(session, flights, subject, limit)


async def create_manual_flight(
    session: AsyncSession,
    pilot_id: int,
    flight_date: date,
    dep_icao: str,
    arr_icao: str,
    out_utc: datetime,
    in_utc: datetime,
    aircraft_id: int | None = None,
) -> Flight:
    """Рейс, которого не было в расписании.

    Перегонка, полёт на чужом типе, работа в другом месте — всё, что
    календарь компании не показывает. Функция остаётся неподтверждённой:
    задания на полёт нет, и вывести её не из чего — пилот задаёт сам.
    """
    flight = Flight(
        pilot_id=pilot_id,
        flight_date=flight_date,
        dep_icao=dep_icao.upper(),
        arr_icao=arr_icao.upper(),
        out_utc=out_utc,
        in_utc=in_utc,
        aircraft_id=aircraft_id,
        block_minutes=max(0, int((in_utc - out_utc).total_seconds() // 60)),
        function=Function.UNVERIFIED,
        source=FlightSource.MANUAL,
    )

    employer = await employer_for(session, pilot_id, flight_date)
    flight.employer_id = employer.id if employer else None

    dep = await coords_for(session, flight.dep_icao)
    arr = await coords_for(session, flight.arr_icao)
    computed = night_minutes(out_utc, in_utc, dep, arr)
    if computed is not None:
        flight.night_minutes = computed
        flight.night_computed = True
        flight.day_landings = 0 if computed else 1
        flight.night_landings = 1 if computed else 0
    else:
        flight.day_landings = 1

    session.add(flight)
    await session.flush()
    return flight


async def crew_of(session: AsyncSession, flight_id: int) -> list[tuple[str, str]]:
    """Экипаж рейса: пары (ФИО, должность), владелец книжки первым."""
    labels = {
        CrewRole.PIC: "КВС",
        CrewRole.SIC: "Второй пилот",
        CrewRole.RELIEF: "Усиление",
        CrewRole.RELIEF2: "Усиление",
        CrewRole.INSTRUCTOR: "Инструктор",
        CrewRole.STUDENT: "Стажёр",
        CrewRole.OBSERVER: "Проверяющий",
        CrewRole.CABIN: "Бортпроводник",
    }
    order = list(labels)
    stmt = select(FlightCrew).where(FlightCrew.flight_id == flight_id)
    links = list((await session.execute(stmt)).scalars().all())
    links.sort(key=lambda link: (order.index(link.role) if link.role in order else 99))
    return [
        (link.person.display, labels.get(link.role, link.role.value))
        for link in links
        if link.person
    ]
