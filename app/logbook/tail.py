"""Подбор регистрации борта.

В ленте расписания приходит только тип самолёта, регистрации нет.
Вводить её каждый раз руками из 117 бортов неудобно, поэтому:

* для разворотного рейса борт почти всегда тот же, что на прилёте —
  предлагается автоматически, но с пометкой, откуда взят;
* для первого рейса смены предлагаются те, на которых пилот летал
  этим маршрутом недавно;
* всегда доступен поиск по обрывку: "73125" находит RA-73125,
  "BCD" находит VP-BCD.

Подстановка остаётся предложением. Регистрация в лётной книжке —
это факт, а не догадка, и подтверждает её пилот.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Aircraft, Flight

# Окно, в пределах которого предыдущий рейс считается той же сменой.
SAME_DUTY_WINDOW = timedelta(hours=18)


@dataclass(slots=True)
class TailSuggestion:
    aircraft: Aircraft
    reason: str          # человекочитаемое объяснение
    confidence: str      # high | medium | low

    @property
    def display(self) -> str:
        return self.aircraft.display


async def search_aircraft(
    session: AsyncSession, fragment: str, limit: int = 10
) -> list[Aircraft]:
    """Поиск по обрывку регистрации, любой из двух.

    Дефисы и регистр игнорируются: "ra73125", "RA-73125" и "73125"
    дают один результат.
    """
    cleaned = fragment.replace("-", "").replace(" ", "").upper()
    if not cleaned:
        return []
    pattern = f"%{cleaned}%"

    # Сравниваем с регистрацией, из которой тоже убран дефис.
    stmt = (
        select(Aircraft)
        .where(
            or_(
                Aircraft.registration.ilike(f"%{fragment}%"),
                Aircraft.registration_ra.ilike(f"%{fragment}%"),
                _normalized(Aircraft.registration).like(pattern),
                _normalized(Aircraft.registration_ra).like(pattern),
            )
        )
        .order_by(Aircraft.registration)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _normalized(column):
    from sqlalchemy import func

    return func.upper(func.replace(column, "-", ""))


async def suggest_tail(
    session: AsyncSession,
    pilot_id: int,
    flight_date: datetime,
    dep_icao: str | None,
    arr_icao: str | None,
    scheduled_out: datetime | None = None,
) -> list[TailSuggestion]:
    """Предложения борта, от самого надёжного к наименее."""
    suggestions: list[TailSuggestion] = []
    seen: set[int] = set()

    def push(aircraft: Aircraft | None, reason: str, confidence: str) -> None:
        if aircraft is None or aircraft.id in seen:
            return
        seen.add(aircraft.id)
        suggestions.append(TailSuggestion(aircraft, reason, confidence))

    moment = scheduled_out or flight_date

    # 1. Разворотный рейс: предыдущий рейс той же смены прилетел туда,
    #    откуда сейчас вылетаем. Борт почти наверняка тот же.
    if dep_icao:
        stmt = (
            select(Flight)
            .where(
                Flight.pilot_id == pilot_id,
                Flight.arr_icao == dep_icao,
                Flight.in_utc.is_not(None),
                Flight.in_utc <= moment,
                Flight.in_utc >= moment - SAME_DUTY_WINDOW,
                Flight.aircraft_id.is_not(None),
            )
            .order_by(Flight.in_utc.desc())
            .limit(1)
        )
        previous = (await session.execute(stmt)).scalar_one_or_none()
        if previous is not None and previous.aircraft:
            hours = (moment - previous.in_utc).total_seconds() / 3600
            push(
                previous.aircraft,
                f"прилетел на нём в {dep_icao} {hours:.0f} ч назад",
                "high",
            )

    # 2. Борта, на которых пилот недавно летал этим же маршрутом.
    if dep_icao and arr_icao:
        stmt = (
            select(Flight)
            .where(
                Flight.pilot_id == pilot_id,
                Flight.dep_icao == dep_icao,
                Flight.arr_icao == arr_icao,
                Flight.aircraft_id.is_not(None),
                Flight.flight_date < moment.date(),
            )
            .order_by(Flight.flight_date.desc())
            .limit(5)
        )
        for flight in (await session.execute(stmt)).scalars().all():
            push(flight.aircraft, f"летали на нём {flight.flight_date:%d.%m.%Y}", "low")

    # 3. Просто последние борта пилота — на случай, когда маршрут новый.
    if len(suggestions) < 3:
        stmt = (
            select(Flight)
            .where(Flight.pilot_id == pilot_id, Flight.aircraft_id.is_not(None))
            .order_by(Flight.flight_date.desc())
            .limit(10)
        )
        for flight in (await session.execute(stmt)).scalars().all():
            push(flight.aircraft, f"последний раз {flight.flight_date:%d.%m.%Y}", "low")

    return suggestions[:6]
