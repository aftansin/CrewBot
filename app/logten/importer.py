"""Запись разобранной книжки в базу.

Импорт идемпотентен: у каждого рейса есть ключ, собранный из даты,
маршрута и времени вылета. Повторный прогон обновляет существующие
записи, а не плодит дубли — это важно, потому что гонять импорт придётся
не один раз, пока не сойдутся все контрольные цифры.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Aircraft,
    Airport,
    CrewRole,
    Employer,
    Flight,
    FlightCrew,
    FlightSource,
    Function,
    Person,
    PersonAlias,
)
from app.logten.function import resolve
from app.logten.reader import Record

# Аэропорты, которых нет в открытом справочнике: закрыты или переименованы.
MANUAL_AIRPORTS = {
    "EDDT": ("Berlin Tegel", 52.5597, 13.2877, "DE", True),
    "URRR": ("Rostov-on-Don", 47.2582, 39.8181, "RU", True),
    "DTNZ": ("Enfidha Hammamet", 36.0758, 10.4387, "TN", False),
}

DEFAULT_EMPLOYERS = (
    ("Трансаэро", date(2011, 1, 1), date(2015, 12, 31)),
    ("Аэрофлот", date(2016, 1, 1), date(2019, 10, 31)),
    ("Переучивание, США", date(2019, 11, 1), date(2020, 2, 13)),
    ("Нордвинд", date(2020, 2, 14), date(2025, 3, 2)),
    ("Аэрофлот", date(2025, 3, 3), None),
)


@dataclass
class ImportStats:
    flights_created: int = 0
    flights_updated: int = 0
    people_created: int = 0
    aircraft_created: int = 0
    airports_created: int = 0
    crew_links: int = 0
    total_minutes: int = 0


def import_key(record: Record) -> str:
    raw = "|".join([
        record.flight_date.isoformat(),
        record.dep_icao or "",
        record.arr_icao or "",
        record.out_utc.isoformat() if record.out_utc else "",
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def split_name(full: str) -> tuple[str, str | None, str | None]:
    parts = full.split()
    if not parts:
        return full, None, None
    return parts[0], (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)


class Importer:
    def __init__(
        self, session: Session, pilot_id: int, owner_hint: str = "Evstifeev"
    ) -> None:
        self.session = session
        # Книжка принадлежит конкретному пилоту: рейсы, люди и работодатели
        # привязаны к нему, справочники бортов и аэропортов общие.
        self.pilot_id = pilot_id
        self.owner_hint = owner_hint
        self.stats = ImportStats()
        self._people: dict[str, Person] = {}
        self._aircraft: dict[str, Aircraft] = {}
        self._airports: set[str] = set()
        self._employers: list[Employer] = []

    # ------------------------------------------------------------------
    # Справочники
    # ------------------------------------------------------------------

    def seed_employers(self) -> None:
        existing = self.session.scalars(
            select(Employer).where(Employer.pilot_id == self.pilot_id)
        ).all()
        if existing:
            self._employers = list(existing)
            return
        for name, start, end in DEFAULT_EMPLOYERS:
            employer = Employer(
                pilot_id=self.pilot_id, name=name, started_on=start, ended_on=end
            )
            self.session.add(employer)
            self._employers.append(employer)
        self.session.flush()

    def seed_airports(self, icao_codes: set[str], catalogue_path: str | None) -> None:
        known = set(self.session.scalars(select(Airport.icao)).all())
        wanted = {c for c in icao_codes if c and c not in known}
        found: dict[str, tuple] = {}

        if catalogue_path and not os.path.exists(catalogue_path):
            # Без справочника импорт не падает: аэропорты заведутся без
            # координат, и ночное время по ним просто не посчитается.
            print(f"ВНИМАНИЕ: справочник аэропортов не найден: {catalogue_path}")
            print("  аэропорты будут заведены без координат")
            catalogue_path = None

        if catalogue_path:
            with open(catalogue_path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    for key in (row.get("icao_code"), row.get("ident"), row.get("gps_code")):
                        if key in wanted and key not in found:
                            with contextlib.suppress(TypeError, ValueError, KeyError):
                                found[key] = (
                                    row.get("name"),
                                    float(row["latitude_deg"]),
                                    float(row["longitude_deg"]),
                                    row.get("iso_country"),
                                    row.get("type") == "closed",
                                    row.get("iata_code") or None,
                                    row.get("municipality") or None,
                                )

        for icao in sorted(wanted):
            if icao in found:
                name, lat, lon, country, closed, iata, city = found[icao]
                airport = Airport(icao=icao, name=name, latitude=lat, longitude=lon,
                                  country=country, closed=closed, iata=iata, municipality=city)
            elif icao in MANUAL_AIRPORTS:
                name, lat, lon, country, closed = MANUAL_AIRPORTS[icao]
                airport = Airport(icao=icao, name=name, latitude=lat, longitude=lon,
                                  country=country, closed=closed)
            else:
                # Координат нет — аэропорт всё равно заводим, иначе рейс
                # потеряет ссылку. Ночное время по нему просто не считается.
                airport = Airport(icao=icao)
            self.session.add(airport)
            self._airports.add(icao)
            self.stats.airports_created += 1
        self.session.flush()

    def person_for(self, name: str) -> Person:
        if name in self._people:
            return self._people[name]

        alias = self.session.scalar(
            select(PersonAlias)
            .join(Person, Person.id == PersonAlias.person_id)
            .where(PersonAlias.alias == name, Person.pilot_id == self.pilot_id)
        )
        if alias is not None:
            self._people[name] = alias.person
            return alias.person

        last, first, middle = split_name(name)
        person = Person(
            pilot_id=self.pilot_id,
            last_name=last,
            first_name=first,
            middle_name=middle,
            is_owner=self.owner_hint in name,
        )
        person.aliases.append(PersonAlias(alias=name))
        self.session.add(person)
        self.session.flush()
        self._people[name] = person
        self.stats.people_created += 1
        return person

    def aircraft_for(self, record: Record) -> Aircraft | None:
        if not record.tail:
            return None
        if record.tail in self._aircraft:
            return self._aircraft[record.tail]

        found = self.session.scalar(
            select(Aircraft).where(Aircraft.registration == record.tail)
        )
        if found is None:
            type_code = None
            type_name = record.aircraft_type
            if type_name and "(" in type_name and type_name.endswith(")"):
                type_name, _, code = type_name.rpartition("(")
                type_code = code.rstrip(")").strip()
                type_name = type_name.strip()
            found = Aircraft(
                registration=record.tail,
                registration_ra=record.tail_ra,
                type_code=type_code,
                type_name=type_name,
                model=record.aircraft_model,
                year_built=record.aircraft_year,
                multi_pilot=not (type_code or "").startswith("C17"),
            )
            self.session.add(found)
            self.session.flush()
            self.stats.aircraft_created += 1
        elif record.tail_ra and not found.registration_ra:
            found.registration_ra = record.tail_ra

        self._aircraft[record.tail] = found
        return found

    def employer_for(self, day: date) -> Employer | None:
        for employer in self._employers:
            if employer.covers(day):
                return employer
        return None

    # ------------------------------------------------------------------
    # Рейсы
    # ------------------------------------------------------------------

    def import_records(self, records: list[Record], catalogue_path: str | None = None) -> ImportStats:
        self.seed_employers()
        codes = {c for r in records for c in (r.dep_icao, r.arr_icao) if c}
        self.seed_airports(codes, catalogue_path)

        for record in records:
            self._import_one(record)

        self.session.flush()
        return self.stats

    def _import_one(self, record: Record) -> None:
        key = import_key(record)
        flight = self.session.scalar(
            select(Flight).where(
                Flight.import_key == key, Flight.pilot_id == self.pilot_id
            )
        )
        created = flight is None
        if flight is None:
            flight = Flight(import_key=key, pilot_id=self.pilot_id)
            self.session.add(flight)

        resolution = resolve(record, self.owner_hint)

        flight.flight_date = record.flight_date
        flight.out_utc = record.out_utc
        flight.in_utc = record.in_utc
        flight.dep_icao = record.dep_icao
        flight.arr_icao = record.arr_icao
        flight.aircraft_id = (a.id if (a := self.aircraft_for(record)) else None)
        flight.employer_id = (e.id if (e := self.employer_for(record.flight_date)) else None)
        flight.flight_number = record.flight_number
        # Блок-тайм берём из книжки как есть: пересчёт из отметок дал бы
        # расхождение на округлениях, а сумма обязана сойтись до минуты.
        flight.block_minutes = record.total_minutes
        flight.function = Function(resolution.function.value)
        flight.function_source = resolution.source
        flight.pilot_flying = _is_owner_flying(record, self.owner_hint)
        flight.night_minutes = record.durations.get("night") or 0
        flight.night_computed = False
        flight.ifr_minutes = record.durations.get("actual_instrument") or 0
        flight.cross_country_minutes = record.durations.get("cross_country") or 0
        flight.dual_received_minutes = record.durations.get("dual_received") or 0
        flight.dual_given_minutes = record.durations.get("dual_given") or 0
        flight.day_takeoffs = record.counts.get("day_takeoffs") or 0
        flight.night_takeoffs = record.counts.get("night_takeoffs") or 0
        flight.day_landings = record.counts.get("day_landings") or 0
        flight.night_landings = record.counts.get("night_landings") or 0
        flight.on_duty_utc = record.on_duty
        flight.off_duty_utc = record.off_duty
        flight.duty_minutes = record.durations.get("duty_total")
        flight.duty_computed = False
        flight.distance_nm = record.distance_nm
        flight.remarks = record.remarks
        flight.source = FlightSource.LOGTEN
        flight.extra = record.raw_extra or None

        self.session.flush()

        existing = {(c.person_id, c.role) for c in flight.crew}
        for slot in record.crew:
            person = self.person_for(slot.name)
            role = CrewRole(slot.role)
            if (person.id, role) in existing:
                continue
            flight.crew.append(
                FlightCrew(
                    person_id=person.id,
                    role=role,
                    pilot_flying=_slot_is_flying(record, slot.role),
                )
            )
            self.stats.crew_links += 1

        self.stats.total_minutes += record.total_minutes
        if created:
            self.stats.flights_created += 1
        else:
            self.stats.flights_updated += 1


def _slot_is_flying(record: Record, role: str) -> bool:
    capacity = (record.pilot_flying or "").upper()
    return bool(capacity) and capacity.startswith(role[:3])


def _is_owner_flying(record: Record, owner_hint: str) -> bool | None:
    if not record.pilot_flying:
        return None
    roles = [s.role for s in record.crew if owner_hint in s.name]
    if not roles:
        return None
    return _slot_is_flying(record, roles[0])
