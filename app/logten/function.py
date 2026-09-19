"""Определение функции пилота в рейсе по правилам EASA.

AMC1 FCL.050 требует различать: PIC (включая solo, SPIC и PICUS),
второго пилота, второго пилота усиленного экипажа, dual, а также
инструктора и экзаменатора. Одного поля "PIC/SIC" для этого мало.

Принцип: функцию НЕ придумываем. Источники в порядке убывания надёжности:

1. роль владельца в составе экипажа, если она записана;
2. проставленное им же время PIC или SIC, если роль пуста;
3. ничего — функция остаётся неподтверждённой.

Время рейса при этом не меняется никогда: сумма по функциям обязана
совпадать с общим налётом до минуты.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.logten.reader import Record

OWNER_HINT = "Evstifeev"


class Function(str, enum.Enum):
    PIC = "pic"                      # командир
    PICUS = "picus"                  # КВС под надзором, засчитывается как PIC
    COPILOT = "copilot"              # второй пилот
    CRUISE_RELIEF = "cruise_relief"  # второй пилот усиленного экипажа
    DUAL = "dual"                    # обучение с инструктором
    FI = "fi"                        # инструктор, засчитывается как PIC
    FE = "fe"                        # экзаменатор, засчитывается как PIC
    UNVERIFIED = "unverified"        # данных нет, в функциональные итоги не идёт


# Функции, которые EASA засчитывает в колонку PIC.
COUNTS_AS_PIC = {Function.PIC, Function.PICUS, Function.FI, Function.FE}
# Функции, которые идут в колонку второго пилота. Время усиленного экипажа
# записывается как время второго пилота, когда пилот занимает пилотское кресло.
COUNTS_AS_COPILOT = {Function.COPILOT, Function.CRUISE_RELIEF}

ROLE_TO_FUNCTION = {
    "PIC": Function.PIC,
    "SIC": Function.COPILOT,
    "RELIEF": Function.CRUISE_RELIEF,
    "RELIEF2": Function.CRUISE_RELIEF,
    "INSTRUCTOR": Function.FI,
    "STUDENT": Function.DUAL,
}


@dataclass(slots=True)
class Resolution:
    function: Function
    source: str          # откуда взято, для отчёта
    minutes: int


def resolve(record: Record, owner_hint: str = OWNER_HINT) -> Resolution:
    minutes = record.total_minutes
    roles = [slot.role for slot in record.crew if owner_hint in slot.name]

    for role in roles:
        mapped = ROLE_TO_FUNCTION.get(role)
        if mapped is not None:
            return Resolution(mapped, f"роль в экипаже: {role}", minutes)

    # Роль OBSERVER означает лишь присутствие в кабине и сама по себе
    # функции не задаёт: проверять могли как его, так и он. Полагаемся
    # на время, которое он проставил сам.
    pic = record.durations.get("pic") or 0
    sic = record.durations.get("sic") or 0

    if pic and not sic:
        return Resolution(Function.PIC, "проставлено время PIC", minutes)
    if sic and not pic:
        return Resolution(Function.COPILOT, "проставлено время SIC", minutes)
    if pic and sic:
        # Смешанный рейс: берём преобладающее, остаток уйдёт в примечание.
        winner = Function.PIC if pic >= sic else Function.COPILOT
        return Resolution(winner, "время PIC и SIC вместе", minutes)

    return Resolution(Function.UNVERIFIED, "нет ни роли, ни времени", minutes)


def buckets(records: list[Record], owner_hint: str = OWNER_HINT):
    """Раскладка налёта по функциям EASA плюс итоговые колонки."""
    per_function: dict[Function, list[int]] = {f: [0, 0] for f in Function}
    sources: dict[str, int] = {}

    for record in records:
        res = resolve(record, owner_hint)
        slot = per_function[res.function]
        slot[0] += 1
        slot[1] += res.minutes
        sources[res.source] = sources.get(res.source, 0) + 1

    pic_total = sum(per_function[f][1] for f in COUNTS_AS_PIC)
    copilot_total = sum(per_function[f][1] for f in COUNTS_AS_COPILOT)
    dual_total = per_function[Function.DUAL][1]
    unverified_total = per_function[Function.UNVERIFIED][1]

    return per_function, sources, {
        "pic": pic_total,
        "copilot": copilot_total,
        "dual": dual_total,
        "unverified": unverified_total,
    }
