"""Слой доступа к данным. Ни один SQL-запрос не живёт вне этого модуля."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, EventKind, EventStatus, Pilot, SyncStatus

# --------------------------------------------------------------------------
# Пилоты
# --------------------------------------------------------------------------


async def get_pilot(session: AsyncSession, pilot_id: int) -> Pilot | None:
    return await session.get(Pilot, pilot_id)


async def get_or_create_pilot(
    session: AsyncSession,
    pilot_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    default_timezone: str,
    default_poll_interval: int,
) -> tuple[Pilot, bool]:
    """Возвращает пилота и флаг "создан только что".

    В старой версии здесь была ошибка: объект после вставки не перечитывался,
    и в хендлеры уходил None.
    """
    pilot = await session.get(Pilot, pilot_id)
    if pilot is not None:
        # Telegram-профиль мог поменяться — держим его актуальным.
        changed = False
        for field, value in (
            ("username", username),
            ("tg_first_name", first_name),
            ("tg_last_name", last_name),
        ):
            if getattr(pilot, field) != value:
                setattr(pilot, field, value)
                changed = True
        if changed:
            await session.flush()
        return pilot, False

    pilot = Pilot(
        id=pilot_id,
        username=username,
        tg_first_name=first_name,
        tg_last_name=last_name,
        timezone=default_timezone,
        poll_interval_minutes=default_poll_interval,
    )
    session.add(pilot)
    await session.flush()
    return pilot, True


async def list_pilots(session: AsyncSession, only_linked: bool = False) -> Sequence[Pilot]:
    stmt = select(Pilot).order_by(Pilot.created_at.desc())
    if only_linked:
        stmt = stmt.where(Pilot.ics_url.is_not(None), Pilot.is_blocked.is_(False))
    result = await session.execute(stmt)
    return result.scalars().all()


async def set_ics_url(session: AsyncSession, pilot: Pilot, url: str | None) -> None:
    pilot.ics_url = url
    pilot.linked_at = datetime.now(tz=ZoneInfo("UTC")) if url else None
    if url is None:
        pilot.last_sync_at = None
        pilot.last_sync_status = None
        pilot.last_sync_error = None
    await session.flush()


async def set_poll_interval(session: AsyncSession, pilot: Pilot, minutes: int) -> None:
    pilot.poll_interval_minutes = minutes
    await session.flush()


async def set_sync_result(
    session: AsyncSession,
    pilot: Pilot,
    status: SyncStatus,
    error: str | None = None,
) -> None:
    pilot.last_sync_at = datetime.now(tz=ZoneInfo("UTC"))
    pilot.last_sync_status = status
    pilot.last_sync_error = error
    await session.flush()


async def update_full_name(
    session: AsyncSession,
    pilot: Pilot,
    last_name: str | None,
    first_name: str | None,
    middle_name: str | None,
) -> None:
    pilot.last_name = last_name
    pilot.first_name = first_name
    pilot.middle_name = middle_name
    await session.flush()


async def delete_pilot(session: AsyncSession, pilot: Pilot) -> None:
    await session.delete(pilot)
    await session.flush()


# --------------------------------------------------------------------------
# События
# --------------------------------------------------------------------------


async def get_all_events(session: AsyncSession, pilot_id: int) -> Sequence[Event]:
    stmt = select(Event).where(Event.pilot_id == pilot_id).order_by(Event.dtstart)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_event(session: AsyncSession, pilot_id: int, event_id: int) -> Event | None:
    stmt = select(Event).where(Event.id == event_id, Event.pilot_id == pilot_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_events_in_range(
    session: AsyncSession,
    pilot_id: int,
    start: datetime,
    end: datetime,
    include_cancelled: bool = False,
) -> Sequence[Event]:
    """Границы обязаны быть timezone-aware.

    В старой версии фильтр строился от naive ``datetime.now()`` против
    aware-колонки — на Postgres это ошибка, на SQLite молчаливый мусор.
    """
    _assert_aware(start)
    _assert_aware(end)

    stmt = (
        select(Event)
        .where(Event.pilot_id == pilot_id, Event.dtstart >= start, Event.dtstart < end)
        .order_by(Event.dtstart)
    )
    if not include_cancelled:
        stmt = stmt.where(Event.status == EventStatus.SCHEDULED)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_upcoming_events(
    session: AsyncSession,
    pilot_id: int,
    since: datetime,
    limit: int | None = None,
) -> Sequence[Event]:
    _assert_aware(since)
    stmt = (
        select(Event)
        .where(
            Event.pilot_id == pilot_id,
            Event.dtend >= since,
            Event.status == EventStatus.SCHEDULED,
        )
        .order_by(Event.dtstart)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_events(session: AsyncSession, pilot_id: int) -> int:
    stmt = select(func.count(Event.id)).where(
        Event.pilot_id == pilot_id, Event.status == EventStatus.SCHEDULED
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_flight_minutes(
    session: AsyncSession,
    pilot_id: int,
    start: datetime,
    end: datetime,
) -> int:
    """Налёт за период в минутах.

    Считается в Python, а не в SQL: у события может быть ручная корректировка
    ``actual_block_minutes``, которая приоритетнее расписания.
    """
    events = await get_events_in_range(session, pilot_id, start, end)
    return sum(e.block_minutes for e in events if e.kind is EventKind.FLIGHT)


def _assert_aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("Ожидается timezone-aware datetime")
