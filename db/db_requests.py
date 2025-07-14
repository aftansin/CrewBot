from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Pilot, Event


async def insert_pilot(
    session: AsyncSession,
    telegram_id: int,
    username: str,
    first_name: str,
    last_name: str | None = None,
):
    """
    Добавление пользователя в таблице pilots
    :param session: сессия СУБД
    :param telegram_id: айди пользователя
    :param username: никнейм
    :param first_name: имя пользователя
    :param last_name: фамилия пользователя
    """
    stmt = upsert(Pilot).values(
        {
            "id": telegram_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
        }
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
    await session.execute(stmt)
    await session.commit()


async def get_db_pilot(session: AsyncSession, telegram_id: int):
    stmt = select(Pilot).where(Pilot.id == telegram_id)
    result = await session.execute(stmt)
    pilot = result.scalar_one_or_none()
    return pilot


async def get_db_pilots(session: AsyncSession):
    stmt = select(Pilot).order_by(Pilot.registration_date.desc())
    result = await session.execute(stmt)
    users = result.scalars().all()
    return users


async def get_pilot_event_by_id(session: AsyncSession, pilot_id: int, event_id: int):
    stmt = select(Event).where(Event.pilot_id == pilot_id, Event.event_id == event_id)
    result = await session.execute(stmt)
    event = result.scalar_one_or_none()
    return event


async def insert_pilot_event(session: AsyncSession, pilot_id: int, event):
    # Создаём новый объект события
    new_event = Event(
        event_id=event.get('event_id'),
        summary=event.get('summary'),
        description=event.get('description'),
        dtstart=event.get('dtstart'),
        dtend=event.get('dtend'),
        pilot_id=pilot_id
    )
    session.add(new_event)  # Добавляем новый объект в сессию
    await session.commit()  # Фиксируем изменения в базе данных
    return new_event


async def update_ics_link(session: AsyncSession, pilot_id: int, ics_url: str | None):
    stmt = update(Pilot).where(Pilot.id == pilot_id).values(ics_url=ics_url)
    await session.execute(stmt)
    await session.commit()


async def update_pilot_full_name(session: AsyncSession, pilot_id: int, full_name: tuple):
    stmt = update(Pilot).where(Pilot.id == pilot_id).values(
        last_name=full_name[0],
        first_name=full_name[1],
        middle_name=full_name[2])
    await session.execute(stmt)
    await session.commit()
