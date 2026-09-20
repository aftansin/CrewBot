"""Резервная копия лётной книжки.

Выгрузка логическая, в JSON: она не зависит от версии Postgres, читается
глазами и восстанавливается нашим же кодом. Для полного отказа сервера
этого мало — там нужен дамп всей базы, он делается на хосте (см. README).
Здесь закрыт другой, более частый случай: потерялся том, ошиблись
командой, испортили данные правкой.

Копия содержит всё, что нельзя восстановить из ленты расписания:
рейсы, экипажи, справочники людей и бортов, периоды работы и историю
правок. Расписание в копию не идёт — оно перезагружается из календаря.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Aircraft,
    Airport,
    Employer,
    Flight,
    FlightCrew,
    FlightRevision,
    Person,
    PersonAlias,
    Pilot,
)

FORMAT_VERSION = 1


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "value"):  # Enum
        return value.value
    return value


def _row(obj: Any, fields: tuple[str, ...]) -> dict:
    return {name: _plain(getattr(obj, name)) for name in fields}


PILOT_FIELDS = (
    "id", "username", "last_name", "first_name", "middle_name",
    "timezone", "poll_interval_minutes", "created_at",
)
FLIGHT_FIELDS = (
    "id", "flight_date", "out_utc", "in_utc", "dep_icao", "arr_icao",
    "aircraft_id", "employer_id", "flight_number", "block_minutes",
    "function", "function_source", "pilot_flying", "night_minutes",
    "night_computed", "ifr_minutes", "cross_country_minutes",
    "dual_received_minutes", "dual_given_minutes", "day_takeoffs",
    "night_takeoffs", "day_landings", "night_landings", "on_duty_utc",
    "off_duty_utc", "duty_minutes", "duty_computed", "distance_nm",
    "remarks", "source", "source_event_uid", "import_key", "extra",
)
PERSON_FIELDS = ("id", "last_name", "first_name", "middle_name", "is_owner", "note")
AIRCRAFT_FIELDS = (
    "id", "registration", "registration_ra", "type_code", "type_name",
    "model", "year_built", "multi_pilot", "note",
)
AIRPORT_FIELDS = ("icao", "iata", "name", "municipality", "country",
                  "latitude", "longitude", "closed")
EMPLOYER_FIELDS = ("id", "name", "started_on", "ended_on", "note")
CREW_FIELDS = ("flight_id", "person_id", "role", "pilot_flying")
REVISION_FIELDS = ("flight_id", "field", "old_value", "new_value", "changed_at")


async def build_backup(session: AsyncSession, pilot_id: int) -> dict:
    """Собирает копию книжки одного пилота."""
    pilot = await session.get(Pilot, pilot_id)

    async def fetch(model, where=None):
        stmt = select(model)
        if where is not None:
            stmt = stmt.where(where)
        return list((await session.execute(stmt)).scalars().all())

    flights = await fetch(Flight, Flight.pilot_id == pilot_id)
    people = await fetch(Person, Person.pilot_id == pilot_id)
    employers = await fetch(Employer, Employer.pilot_id == pilot_id)
    flight_ids = [f.id for f in flights]
    person_ids = [p.id for p in people]

    crew = (
        await fetch(FlightCrew, FlightCrew.flight_id.in_(flight_ids))
        if flight_ids
        else []
    )
    revisions = (
        await fetch(FlightRevision, FlightRevision.flight_id.in_(flight_ids))
        if flight_ids
        else []
    )
    aliases = (
        await fetch(PersonAlias, PersonAlias.person_id.in_(person_ids))
        if person_ids
        else []
    )
    # Справочники бортов и аэропортов общие, но без них копия неполна:
    # рейсы ссылаются на них по идентификатору.
    aircraft = await fetch(Aircraft)
    airports = await fetch(Airport)

    total_block = sum(f.block_minutes for f in flights)

    return {
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now().isoformat(),
        "pilot": _row(pilot, PILOT_FIELDS) if pilot else None,
        # Контрольные цифры: по ним видно, что копия целая, ещё до попытки
        # восстановления.
        "checksums": {
            "flights": len(flights),
            "block_minutes": total_block,
            "block_hhmm": f"{total_block // 60}:{total_block % 60:02d}",
            "people": len(people),
            "aircraft": len(aircraft),
            "crew_links": len(crew),
            "revisions": len(revisions),
        },
        "employers": [_row(e, EMPLOYER_FIELDS) for e in employers],
        "aircraft": [_row(a, AIRCRAFT_FIELDS) for a in aircraft],
        "airports": [_row(a, AIRPORT_FIELDS) for a in airports],
        "people": [_row(p, PERSON_FIELDS) for p in people],
        "person_aliases": [
            {"person_id": a.person_id, "alias": a.alias} for a in aliases
        ],
        "flights": [_row(f, FLIGHT_FIELDS) for f in flights],
        "crew": [_row(c, CREW_FIELDS) for c in crew],
        "revisions": [_row(r, REVISION_FIELDS) for r in revisions],
    }


def to_bytes(backup: dict) -> bytes:
    return json.dumps(backup, ensure_ascii=False, indent=1).encode("utf-8")


def summary_text(backup: dict) -> str:
    checks = backup["checksums"]
    return (
        f"Рейсов: {checks['flights']}\n"
        f"Налёт: {checks['block_hhmm']}\n"
        f"Людей: {checks['people']}   бортов: {checks['aircraft']}\n"
        f"Связей экипажа: {checks['crew_links']}   правок: {checks['revisions']}"
    )


def filename_for(pilot_id: int, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    return f"logbook-{pilot_id}-{stamp}.json"


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_COLUMNS = (
    "date", "flight_number", "from_icao", "to_icao", "aircraft",
    "aircraft_type", "out_utc", "in_utc", "block_hhmm", "block_minutes",
    "night_hhmm", "function", "pic_name", "sic_name", "crew",
    "employer", "day_landings", "night_landings", "duty_hhmm",
    "remarks", "source",
)

CSV_ROLE_ORDER = ("PIC", "SIC", "RELIEF", "RELIEF2", "INSTRUCTOR",
                  "STUDENT", "OBSERVER", "CABIN")


def _hhmm(minutes: int | None) -> str:
    total = minutes or 0
    return f"{total // 60}:{total % 60:02d}"


async def build_csv(session: AsyncSession, pilot_id: int) -> bytes:
    """Плоская выгрузка рейсов в CSV.

    В отличие от JSON, это не резервная копия для восстановления, а
    таблица для Excel и для переноса в другие программы: одна строка —
    один рейс, экипаж собран в одну ячейку.

    Разделитель — точка с запятой, кодировка с BOM: иначе Excel
    открывает файл одной колонкой и портит кириллицу.
    """
    import csv
    import io

    stmt = (
        select(Flight)
        .where(Flight.pilot_id == pilot_id)
        .order_by(Flight.flight_date, Flight.out_utc)
    )
    flights = list((await session.execute(stmt)).scalars().all())

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    for flight in flights:
        by_role: dict[str, list[str]] = {}
        for link in flight.crew:
            if link.person:
                by_role.setdefault(link.role.value, []).append(link.person.display)

        crew_text = "; ".join(
            f"{role}: {', '.join(names)}"
            for role in CSV_ROLE_ORDER
            if (names := by_role.get(role))
        )

        writer.writerow([
            flight.flight_date.isoformat(),
            flight.flight_number or "",
            flight.dep_icao or "",
            flight.arr_icao or "",
            flight.aircraft.display if flight.aircraft else "",
            (flight.aircraft.type_name or "") if flight.aircraft else "",
            flight.out_utc.strftime("%H:%M") if flight.out_utc else "",
            flight.in_utc.strftime("%H:%M") if flight.in_utc else "",
            _hhmm(flight.block_minutes),
            flight.block_minutes or 0,
            _hhmm(flight.night_minutes),
            flight.function.value,
            ", ".join(by_role.get("PIC", [])),
            ", ".join(by_role.get("SIC", [])),
            crew_text,
            flight.employer.name if flight.employer else "",
            flight.day_landings or 0,
            flight.night_landings or 0,
            _hhmm(flight.duty_minutes),
            (flight.remarks or "").replace("\n", " "),
            flight.source.value,
        ])

    return buffer.getvalue().encode("utf-8-sig")


def csv_filename(pilot_id: int, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    return f"logbook-{pilot_id}-{stamp}.csv"
