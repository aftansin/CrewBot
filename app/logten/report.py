"""Отчёт предпросмотра перед импортом.

Печатает то, по чему импорт можно принять или отклонить: сколько записей
разобрано, суммарный налёт, разбивка по компаниям, и — главное — полный
перечень строк, которые не удалось разобрать. Ни одна проблема не
замалчивается: логбук единственный источник данных, тихая потеря
недопустима.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from app.logten.reader import Record
from app.logten.values import fmt_minutes


@dataclass(frozen=True, slots=True)
class Employer:
    name: str
    start: date
    end: date | None

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


# Периоды работы. Границы Трансаэро и американского периода выведены
# из данных (префиксы регистраций и паузы), Нордвинд назван владельцем.
EMPLOYERS: tuple[Employer, ...] = (
    Employer("Трансаэро", date(2011, 1, 1), date(2015, 12, 31)),
    Employer("Аэрофлот (1)", date(2016, 1, 1), date(2019, 10, 31)),
    Employer("США, C172", date(2019, 11, 1), date(2020, 2, 13)),
    Employer("Нордвинд", date(2020, 2, 14), date(2025, 3, 2)),
    Employer("Аэрофлот (2)", date(2025, 3, 3), None),
)


def employer_for(day: date) -> str:
    for employer in EMPLOYERS:
        if employer.covers(day):
            return employer.name
    return "вне периодов"


OWNER_HINT = "Evstifeev"


@dataclass
class Summary:
    records: int = 0
    total_minutes: int = 0
    by_employer: dict[str, list[int]] = None  # name -> [рейсов, минут, pic]
    by_type: Counter = None
    by_role: Counter = None
    no_tail: int = 0
    no_owner_role: int = 0
    people: set = None
    aircraft: dict = None


def build(records: list[Record], owner_hint: str = OWNER_HINT) -> Summary:
    s = Summary(
        by_employer=defaultdict(lambda: [0, 0, 0]),
        by_type=Counter(),
        by_role=Counter(),
        people=set(),
        aircraft={},
    )
    for rec in records:
        s.records += 1
        s.total_minutes += rec.total_minutes

        bucket = s.by_employer[employer_for(rec.flight_date)]
        bucket[0] += 1
        bucket[1] += rec.total_minutes
        bucket[2] += rec.durations.get("pic") or 0

        s.by_type[rec.aircraft_type or "— не указан —"] += 1
        if not rec.tail:
            s.no_tail += 1
        elif rec.tail not in s.aircraft:
            s.aircraft[rec.tail] = rec.tail_ra

        owner_roles = [c.role for c in rec.crew if owner_hint in c.name]
        if owner_roles:
            s.by_role[owner_roles[0]] += 1
        else:
            s.no_owner_role += 1

        for slot in rec.crew:
            s.people.add(slot.name)

    return s


def render(records, problems, summary: Summary, expected=None) -> str:
    out: list[str] = []
    add = out.append

    add("=" * 66)
    add("ПРЕДПРОСМОТР ИМПОРТА — в базу пока ничего не записано")
    add("=" * 66)
    add(f"разобрано записей : {summary.records}")
    add(f"суммарный налёт   : {fmt_minutes(summary.total_minutes)}"
        f"  ({summary.total_minutes // 60} ч {summary.total_minutes % 60} мин)")
    add(f"людей в экипажах  : {len(summary.people)}")
    add(f"бортов            : {len(summary.aircraft)}"
        f"   (из них с RA-номером: {sum(1 for v in summary.aircraft.values() if v)})")
    add(f"рейсов без борта  : {summary.no_tail}")
    add("")

    add("НАЛЁТ ПО КОМПАНИЯМ")
    for employer in EMPLOYERS:
        n, minutes, pic = summary.by_employer.get(employer.name, [0, 0, 0])
        if n:
            add(f"  {employer.name:14} {n:5} рейсов  {fmt_minutes(minutes):>9}"
                f"   PIC {fmt_minutes(pic):>9}")
    stray = summary.by_employer.get("вне периодов")
    if stray and stray[0]:
        add(f"  ВНЕ ПЕРИОДОВ   {stray[0]:5} рейсов  {fmt_minutes(stray[1]):>9}  <-- проверить")
    add("")

    add("ТИПЫ ВС")
    for name, count in summary.by_type.most_common():
        add(f"  {count:5}  {name}")
    add("")

    add("РОЛЬ ВЛАДЕЛЬЦА В РЕЙСЕ")
    for role, count in summary.by_role.most_common():
        add(f"  {count:5}  {role}")
    if summary.no_owner_role:
        add(f"  {summary.no_owner_role:5}  роль не определена — импортируется без функции")
    add("")

    if problems:
        add(f"ПРОБЛЕМНЫЕ ЗНАЧЕНИЯ: {len(problems)}")
        shown = Counter((p.column, p.message) for p in problems)
        for (column, message), count in shown.most_common(20):
            example = next(p for p in problems if p.column == column and p.message == message)
            add(f"  {count:5}  {column}: {message}  (строка {example.line}, {example.value!r})")
    else:
        add("ПРОБЛЕМНЫХ ЗНАЧЕНИЙ НЕТ")
    add("")

    if expected:
        add("СВЕРКА С КОНТРОЛЬНЫМИ ЦИФРАМИ")
        ok = True
        for label, want, got in expected:
            mark = "OK " if want == got else "!!!"
            if want != got:
                ok = False
            add(f"  {mark} {label:26} ожидалось {want!s:>12}   получено {got!s:>12}")
        add("")
        add("ИТОГ: импорт можно принимать" if ok else "ИТОГ: НЕ СОВПАЛО, импорт отклонён")
    return "\n".join(out)
