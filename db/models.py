from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, BigInteger, String, DateTime, func, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Определяем модели для таблицы
class Pilot(Base):
    __tablename__ = 'pilot'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String)
    first_name: Mapped[str | None] = mapped_column(String)
    middle_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    registration_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    subscription_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ics_url: Mapped[str | None] = mapped_column(String)

    events: Mapped[list['Event']] = relationship("Event", back_populates="pilot")  # для связи ORM

    def __str__(self) -> str:
        return f'<Pilot: {self.last_name} {self.first_name} {self.middle_name}>'


class Event(Base):
    __tablename__ = 'event'

    uuid: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=func.gen_random_uuid())
    event_id: Mapped[int] = mapped_column(BigInteger)
    pilot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("pilot.id"))
    summary: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dtend: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    pilot: Mapped[Pilot] = relationship("Pilot", back_populates="events")  # для связи ORM
