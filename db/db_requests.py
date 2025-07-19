import asyncio
import zoneinfo
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update, and_
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

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
    session.add(event)  # Добавляем новый объект в сессию
    await session.commit()  # Фиксируем изменения в базе данных
    return event


async def get_pilot_events_from_yesterday_ascending(session: AsyncSession, pilot_id: int):
    # Получаем текущую дату и вычисляем вчерашний день
    today = datetime.now()
    yesterday = today - timedelta(days=1)

    # Формируем запрос: события для указанного пилота, начиная со вчерашнего дня,
    # отсортированные по возрастанию dtstart
    stmt = (
        select(Event)
        .where(
            and_(
                Event.pilot_id == pilot_id,
                Event.dtstart >= yesterday
            )
        )
        .order_by(Event.dtstart.asc())
    )

    result = await session.execute(stmt)
    events = result.scalars().all()
    return events


async def get_pilot_events(session: AsyncSession, pilot_id: int):
    stmt = (select(Event).where(Event.pilot_id == pilot_id)).order_by(Event.dtstart.asc())
    result = await session.execute(stmt)
    events = result.scalars().all()
    return events


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


async def test():
    engine = create_async_engine(url='sqlite+aiosqlite:///database.db', echo=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(zoneinfo.ZoneInfo('Europe/Moscow'))
    async with sessionmaker() as session:
        pilot_events = await get_pilot_events_from_yesterday_ascending(session, 438391677)
        for e in pilot_events:
            print(e.dtstart.replace(tzinfo=zoneinfo.ZoneInfo('Europe/Moscow')))
            dtstart = e.dtstart.replace(tzinfo=zoneinfo.ZoneInfo('Europe/Moscow'))
            print(now)
            print('##########')
            print(e.dtstart > now)
        # existing_events = {e.event_id: e for e in pilot_events if e.dtstart >= now}
        # print(existing_events)

if __name__ == "__main__":
    asyncio.run(test())