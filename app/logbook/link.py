"""Связь записи в книжке с событием расписания.

Правило, из которого всё следует: **после создания запись принадлежит
пилоту, а не календарю.** План правится задним числом — рейс вернулся,
ушёл на запасной, отменился, переставили время, — и книжка не должна
меняться вслед за ним сама.

Поэтому запись хранит снимок плана на момент создания. Позже, когда
лента обновится, расхождение можно обнаружить и показать пилоту. Решает
он: принять новый план, оставить как есть или пометить, что фактически
всё было иначе.

Рейс без события тоже полноправен: перегонка, вылет на запасной вторым
плечом, замена в последний момент — всё это в расписании не появляется.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models import Event, Flight


def snapshot_of(
    event: Event, dep_icao: str | None = None, arr_icao: str | None = None
) -> dict:
    """Снимок значимых полей плана. Хранится в записи навсегда.

    dep_icao и arr_icao — коды, уже переведённые в ICAO по справочнику.
    Без них в снимок попадёт IATA из ленты, и сравнение с записью книжки
    объявит нормальный рейс уходом на запасной.
    """
    return {
        "uid": event.uid,
        "summary": " ".join(event.summary.split())[:200],
        "flight_no": event.flight_no,
        "dep_code": dep_icao or event.dep_code,
        "arr_code": arr_icao or event.arr_code,
        "aircraft_type": event.aircraft,
        "dtstart": event.dtstart.isoformat() if event.dtstart else None,
        "dtend": event.dtend.isoformat() if event.dtend else None,
        "content_hash": event.content_hash,
        "captured_at": datetime.now(tz=event.dtstart.tzinfo).isoformat()
        if event.dtstart
        else None,
    }


@dataclass(slots=True)
class Divergence:
    field: str
    label: str
    planned_then: object
    planned_now: object


FIELD_LABELS = {
    "flight_no": "номер рейса",
    "dep_code": "аэропорт вылета",
    "arr_code": "аэропорт прилёта",
    "aircraft_type": "тип борта",
    "dtstart": "плановое отправление",
    "dtend": "плановое прибытие",
}


def compare_to_plan(flight: Flight, event: Event | None) -> list[Divergence]:
    """Что изменилось в плане с тех пор, как запись была создана.

    Сравниваются два состояния ПЛАНА, а не план с фактом. Фактические
    времена пилота тут ни при чём: они могут законно отличаться от
    расписания, это норма, а не расхождение.
    """
    snapshot = flight.plan_snapshot or {}
    if not snapshot:
        return []

    if event is None:
        # Событие исчезло из ленты. Само по себе это не значит, что рейса
        # не было: лента показывает ограниченное окно и старое выпадает.
        return []

    current = snapshot_of(event)
    if snapshot.get("content_hash") == current.get("content_hash"):
        return []

    result: list[Divergence] = []
    for field, label in FIELD_LABELS.items():
        was = snapshot.get(field)
        now = current.get(field)
        if was != now:
            result.append(Divergence(field, label, was, now))
    return result


def event_was_cancelled(flight: Flight, event: Event | None) -> bool:
    """План отменён после того, как рейс записан в книжку.

    Повод спросить пилота, а не удалять запись. Рейс мог состояться,
    а отменённым оказаться только плановое задание.
    """
    from app.db.models import EventStatus

    if event is None or not flight.plan_snapshot:
        return False
    return event.status is EventStatus.CANCELLED


@dataclass(slots=True)
class ActualVsPlan:
    """Насколько факт разошёлся с планом. Нужно, чтобы подсветить форс-мажор."""

    diverted: bool           # сели не там, где планировали
    air_return: bool         # вернулись туда, откуда вылетели
    delay_minutes: int | None
    longer_minutes: int | None


def compare_actual(flight: Flight) -> ActualVsPlan:
    snapshot = flight.plan_snapshot or {}
    planned_arr = snapshot.get("arr_code")
    planned_dep = snapshot.get("dep_code")

    diverted = bool(
        planned_arr and flight.arr_icao and not _same_airport(planned_arr, flight.arr_icao)
    )
    air_return = bool(
        flight.dep_icao and flight.arr_icao and flight.dep_icao == flight.arr_icao
        and planned_arr and planned_dep and planned_arr != planned_dep
    )

    delay = None
    planned_out = snapshot.get("dtstart")
    if planned_out and flight.out_utc:
        planned_dt = datetime.fromisoformat(planned_out)
        delay = int((flight.out_utc - planned_dt).total_seconds() // 60)

    longer = None
    if snapshot.get("dtstart") and snapshot.get("dtend") and flight.block_minutes:
        planned_block = int(
            (
                datetime.fromisoformat(snapshot["dtend"])
                - datetime.fromisoformat(snapshot["dtstart"])
            ).total_seconds()
            // 60
        )
        longer = flight.block_minutes - planned_block

    return ActualVsPlan(
        diverted=diverted, air_return=air_return, delay_minutes=delay, longer_minutes=longer
    )


def _same_airport(code_a: str, code_b: str) -> bool:
    """Точное сравнение. Оба кода к этому моменту уже в ICAO.

    Выводить ICAO из IATA по виду строки нельзя: связи между ними нет.
    SVO соответствует UUEE, CAI соответствует HECA, IKT соответствует UIII —
    ни одной общей буквы. Перевод делается один раз, при создании
    черновика, по справочнику аэропортов (см. draft.resolve_airports).
    """
    return code_a.upper() == code_b.upper()
