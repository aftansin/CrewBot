"""Чтение выгрузки LogTen в структурированные записи.

Ничего не домысливает: поле, которого нет в файле, остаётся None.
Строка, которую не удалось разобрать, не пропускается молча, а попадает
в список проблем — импортёр обязан показать их пользователю.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime

from app.logten import values as v

# Роли экипажа: колонка выгрузки -> код роли.
CREW_COLUMNS = {
    "flight_selectedCrewPIC": "PIC",
    "flight_selectedCrewSIC": "SIC",
    "flight_selectedCrewRelief": "RELIEF",
    "flight_selectedCrewRelief2": "RELIEF2",
    "flight_selectedCrewInstructor": "INSTRUCTOR",
    "flight_selectedCrewStudent": "STUDENT",
    "flight_selectedCrewObserver": "OBSERVER",
    "flight_selectedCrewFlightAttendant": "CABIN",
}

# Поля с длительностями, переносимые как есть.
DURATION_COLUMNS = {
    "total": "flight_totalTime",
    "pic": "flight_pic",
    "sic": "flight_sic",
    "night": "flight_night",
    "pic_night": "flight_picNight",
    "sic_night": "flight_sicNight",
    "cross_country": "flight_crossCountry",
    "actual_instrument": "flight_actualInstrument",
    "simulated_instrument": "flight_simulatedInstrument",
    "dual_received": "flight_dualReceived",
    "dual_given": "flight_dualGiven",
    "duty_total": "flight_totalDutyTime",
    "rest": "flight_restTime",
}

COUNT_COLUMNS = {
    "day_takeoffs": "flight_dayTakeoffs",
    "night_takeoffs": "flight_nightTakeoffs",
    "day_landings": "flight_dayLandings",
    "night_landings": "flight_nightLandings",
    "legs": "flight_legCount",
}


@dataclass(slots=True)
class Problem:
    line: int
    column: str
    value: str
    message: str


@dataclass(slots=True)
class CrewSlot:
    role: str
    name: str


@dataclass(slots=True)
class Record:
    line: int
    flight_date: date
    dep_icao: str | None
    arr_icao: str | None
    out_utc: datetime | None
    in_utc: datetime | None
    tail: str | None
    tail_ra: str | None
    aircraft_type: str | None
    aircraft_model: str | None
    aircraft_year: int | None
    flight_number: str | None
    durations: dict[str, int | None] = field(default_factory=dict)
    counts: dict[str, int | None] = field(default_factory=dict)
    crew: list[CrewSlot] = field(default_factory=list)
    pilot_flying: str | None = None
    landing_capacity: str | None = None
    remarks: str | None = None
    distance_nm: float | None = None
    on_duty: datetime | None = None
    off_duty: datetime | None = None
    raw_extra: dict[str, str] = field(default_factory=dict)

    @property
    def total_minutes(self) -> int:
        return self.durations.get("total") or 0


# Колонки, которые мы разобрали осознанно. Остальное непустое уходит
# в raw_extra, чтобы ничего не потерялось при переносе.
KNOWN = (
    {"flight_flightDate", "flight_from", "flight_to", "flight_actualDepartureTime",
     "flight_actualArrivalTime", "aircraft_aircraftID", "aircraft_secondaryID",
     "aircraftType_type", "aircraft_aircraftModel", "aircraft_year", "flight_flightNumber",
     "flight_pilotFlyingCapacity", "flight_landingCapacity", "flight_remarks",
     "flight_distance", "flight_onDutyTime", "flight_offDutyTime"}
    | set(CREW_COLUMNS)
    | set(DURATION_COLUMNS.values())
    | set(COUNT_COLUMNS.values())
)


class Reader:
    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[Problem] = []
        self.header: list[str] = []

    def read(self) -> list[Record]:
        with open(self.path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh, delimiter="\t"))
        if not rows:
            return []

        self.header = [h.strip() for h in rows[0]]
        index = {name: i for i, name in enumerate(self.header)}
        records: list[Record] = []

        for line_no, row in enumerate(rows[1:], start=2):
            # Явная привязка row и line_no: замыкание над переменной цикла —
            # классический источник ошибок, даже когда вызов происходит сразу.
            def cell(name: str, _row=row) -> str | None:
                i = index.get(name)
                return _row[i] if i is not None and i < len(_row) else None

            def parse(fn, name: str, default=None, _line=line_no):
                try:
                    return fn(cell(name))
                except v.ValueError_ as exc:
                    self.problems.append(
                        Problem(_line, name, (cell(name) or "").strip(), str(exc))
                    )
                    return default

            day = parse(v.flight_date, "flight_flightDate")
            if day is None:
                self.problems.append(
                    Problem(line_no, "flight_flightDate", "", "нет даты, строка пропущена")
                )
                continue

            dep_t = parse(v.clock, "flight_actualDepartureTime")
            arr_t = parse(v.clock, "flight_actualArrivalTime")
            out_utc, in_utc = v.departure_arrival(day, dep_t, arr_t)

            on_t = parse(v.clock, "flight_onDutyTime")
            off_t = parse(v.clock, "flight_offDutyTime")
            on_duty, off_duty = v.departure_arrival(day, on_t, off_t)

            record = Record(
                line=line_no,
                flight_date=day,
                dep_icao=v.text(cell("flight_from")),
                arr_icao=v.text(cell("flight_to")),
                out_utc=out_utc,
                in_utc=in_utc,
                tail=v.text(cell("aircraft_aircraftID")),
                tail_ra=v.text(cell("aircraft_secondaryID")),
                aircraft_type=v.text(cell("aircraftType_type")),
                aircraft_model=v.text(cell("aircraft_aircraftModel")),
                aircraft_year=parse(v.year_from_timestamp, "aircraft_year"),
                flight_number=v.text(cell("flight_flightNumber")),
                pilot_flying=v.text(cell("flight_pilotFlyingCapacity")),
                landing_capacity=v.text(cell("flight_landingCapacity")),
                remarks=v.text(cell("flight_remarks")),
                distance_nm=parse(v.decimal, "flight_distance"),
                on_duty=on_duty,
                off_duty=off_duty,
            )

            for key, column in DURATION_COLUMNS.items():
                record.durations[key] = parse(v.duration_minutes, column)
            for key, column in COUNT_COLUMNS.items():
                record.counts[key] = parse(v.integer, column)

            for column, role in CREW_COLUMNS.items():
                name = v.text(cell(column))
                if name:
                    record.crew.append(CrewSlot(role=role, name=name))

            for name in self.header:
                if name in KNOWN:
                    continue
                value = v.text(cell(name))
                if value:
                    record.raw_extra[name] = value

            records.append(record)

        return records
