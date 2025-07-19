import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ForeignKey, BigInteger, String, DateTime, func, Index
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
    registration_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.now(),
        server_default=func.now())
    subscription_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ics_url: Mapped[str | None] = mapped_column(String)

    events: Mapped[list['Event']] = relationship("Event", back_populates="pilot")  # для связи ORM

    def __str__(self) -> str:
        return f'<Pilot: {self.last_name} {self.first_name} {self.middle_name}>'


class Event(Base):
    __tablename__ = 'event'

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Используем event_id как primary key
    pilot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("pilot.id"))
    summary: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    dtstart: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo('Europe/Moscow'))
    )
    dtend: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(ZoneInfo('Europe/Moscow'))
    )
    hash: Mapped[str] = mapped_column(String(32))  # Для отслеживания изменений

    pilot: Mapped[Pilot] = relationship("Pilot", back_populates="events")  # для связи ORM

    __table_args__ = (
        Index('idx_event_pilot_id', 'pilot_id', 'event_id'),  # Составной индекс для быстрого поиска
    )

    @property
    def short_summary(self) -> str:
        return get_short_summary(self)


def get_short_summary(event: Event) -> str:
    # TODO Доработать
    date_str = event.dtstart.strftime('%d.%m.%y')

    # Обработка полетов (содержит ✈️)
    if '✈️' in event.summary:
        # Разбиваем строку по стрелке →
        parts = event.summary.split('→')
        if len(parts) >= 2:
            departure_part = parts[0]
            arrival_part = parts[1]

            # Обрабатываем вылет
            dep_city = departure_part.split('(')[0].strip()

            # Обрабатываем прилет
            arr_city = arrival_part.split('(')[0].strip()

            # Формируем результат
            return f"{date_str} {dep_city} → {arr_city}"

    # Обработка медицинских комиссий
    if event.summary.startswith('💉'):
        return f"{date_str} {event.summary.split('\n')[0].strip()}"

    # Обработка тренажеров
    if event.summary.startswith('📋'):
        return f"{date_str} {event.summary.split('\n')[0].strip()}"

    # Для других событий
    first_line = event.summary.split('\n')[0][:20].strip()
    if len(event.summary.split('\n')[0]) > 20:
        first_line += '...'
    return f"{date_str} {first_line}"
