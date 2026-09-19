"""Мост между расписанием и лётной книжкой.

Событие календаря уже содержит номер рейса, тип борта, маршрут и состав
экипажа. Из него собирается черновик записи, в котором пилоту остаётся
вбить только фактические времена запуска и выключения — то, чего в
расписании нет и быть не может.

Ничего не сохраняется автоматически: черновик показывается, пилот его
подтверждает. Лётная книжка не должна заполняться сама по плановым
данным — в ней обязаны стоять фактические.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.db.models import Event, EventKind
from app.icalendar_feed.parse import parse_crew

# Рабочее время: час до планового отправления и полчаса после выключения.
DUTY_BEFORE_DEPARTURE = timedelta(hours=1)
DUTY_AFTER_ARRIVAL = timedelta(minutes=30)


@dataclass(slots=True)
class CrewDraft:
    last_name: str
    first_name: str | None
    middle_name: str | None
    position: str          # КВС, 2П, СБ — как пришло из ленты
    is_owner: bool = False

    @property
    def display(self) -> str:
        parts = [p for p in (self.last_name, self.first_name, self.middle_name) if p]
        return " ".join(parts)


@dataclass(slots=True)
class FlightDraft:
    """Заготовка записи. Поля, которых в расписании нет, остаются None."""

    event_uid: str
    flight_date: datetime
    dep_icao: str | None
    arr_icao: str | None
    flight_number: str | None
    aircraft_type: str | None

    # Плановые времена — из ленты. Идут в расчёт рабочего времени,
    # но НЕ в налёт.
    scheduled_out: datetime | None = None
    scheduled_in: datetime | None = None

    # Фактические — заполняет пилот.
    actual_out: datetime | None = None
    actual_in: datetime | None = None

    # Регистрации в ленте нет, только тип. Подставляется из истории
    # или вводится вручную.
    tail: str | None = None
    tail_suggested_from: str | None = None

    crew: list[CrewDraft] = field(default_factory=list)
    remarks: str | None = None

    @property
    def is_complete(self) -> bool:
        """Готов ли черновик к сохранению."""
        return self.actual_out is not None and self.actual_in is not None

    @property
    def block_minutes(self) -> int | None:
        if self.actual_out is None or self.actual_in is None:
            return None
        return max(0, int((self.actual_in - self.actual_out).total_seconds() // 60))

    @property
    def missing(self) -> list[str]:
        """Чего не хватает — для подсказки в интерфейсе."""
        gaps = []
        if self.actual_out is None:
            gaps.append("время запуска")
        if self.actual_in is None:
            gaps.append("время выключения")
        if not self.tail:
            gaps.append("регистрация борта")
        return gaps

    def duty_window(self) -> tuple[datetime | None, datetime | None]:
        """Рабочее время: час до планового отправления, полчаса после выключения.

        Начало берётся от ПЛАНОВОГО отправления — так считается явка.
        Конец от ФАКТИЧЕСКОГО выключения, потому что смена кончается
        по факту, а не по расписанию.
        """
        start = self.scheduled_out - DUTY_BEFORE_DEPARTURE if self.scheduled_out else None
        end = self.actual_in + DUTY_AFTER_ARRIVAL if self.actual_in else None
        return start, end

    @property
    def duty_minutes(self) -> int | None:
        start, end = self.duty_window()
        if start is None or end is None:
            return None
        return max(0, int((end - start).total_seconds() // 60))


def draft_from_event(event: Event, owner_last_name: str | None = None) -> FlightDraft | None:
    """Собирает черновик из события расписания.

    Возвращает None для всего, что не является рейсом: медкомиссия,
    учёба и явка в лётную книжку не попадают.
    """
    if event.kind is not EventKind.FLIGHT:
        return None

    draft = FlightDraft(
        event_uid=event.uid,
        flight_date=event.dtstart,
        dep_icao=None,
        arr_icao=None,
        flight_number=event.flight_no,
        aircraft_type=event.aircraft,
        scheduled_out=event.dtstart,
        scheduled_in=event.dtend,
    )

    # В ленте коды IATA, в книжке ICAO. Здесь они кладутся как есть,
    # перевод выполняет resolve_airports — ему нужен справочник.
    draft.dep_icao = event.dep_code
    draft.arr_icao = event.arr_code

    for last, first, middle, position in parse_crew(event.description):
        draft.crew.append(
            CrewDraft(
                last_name=last,
                first_name=first or None,
                middle_name=middle or None,
                position=position,
                is_owner=bool(owner_last_name) and last == owner_last_name,
            )
        )

    return draft


def resolve_airports(draft: FlightDraft, iata_to_icao: dict[str, str]) -> FlightDraft:
    """Переводит коды аэропортов из IATA в ICAO по справочнику.

    Вывести ICAO из IATA по виду строки невозможно: соответствие
    произвольное. Код, которого нет в справочнике, остаётся как пришёл —
    подменять его догадкой нельзя, аэропорт в лётной книжке это факт.
    """
    for field_name in ("dep_icao", "arr_icao"):
        code = getattr(draft, field_name)
        if code and len(code) == 3:
            setattr(draft, field_name, iata_to_icao.get(code.upper(), code))
    return draft


def owner_position(draft: FlightDraft) -> str | None:
    """Должность владельца в этом рейсе, как записана в ленте."""
    for member in draft.crew:
        if member.is_owner:
            return member.position
    return None


POSITION_TO_FUNCTION = {
    "КВС": "pic",
    "2П": "copilot",
    "ИНС": "fi",
    "ПИ": "fe",
}


def suggested_function(draft: FlightDraft) -> str | None:
    """Функция, предлагаемая по должности из задания на полёт.

    Это предложение, а не решение: подтверждает пилот. Усиленный экипаж
    лента отдельно не помечает, такие рейсы придётся отмечать руками.
    """
    position = owner_position(draft)
    if position is None:
        return None
    return POSITION_TO_FUNCTION.get(position)
