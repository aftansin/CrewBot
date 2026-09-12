"""Модели данных.

Ключевые отличия от старой схемы:

* у события синтетический первичный ключ, а уникальность — по паре
  ``(pilot_id, uid)``. Один и тот же рейс у двух пилотов больше не конфликтует;
* событие не удаляется при исчезновении из ленты, а помечается
  ``status = cancelled``. История нужна для подсчёта налёта за прошлые месяцы,
  и это делает синхронизацию идемпотентной: если событие вернулось, статус
  просто переключается обратно;
* тип события (рейс / медкомиссия / учёба / явка) — отдельная колонка,
  а не поиск эмодзи в тексте по всему коду.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class EventKind(str, enum.Enum):
    FLIGHT = "flight"          # рейс
    SIMULATOR = "simulator"    # тренажёр
    TRAINING = "training"      # учёба, теория
    MEDICAL = "medical"        # медкомиссия, ВЛЭК
    REPORTING = "reporting"    # явка, reporting
    STANDBY = "standby"        # резерв
    DUTY = "duty"              # прочая наземная работа
    REST = "rest"              # выходной, отпуск
    OTHER = "other"


class EventStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class SyncStatus(str, enum.Enum):
    OK = "ok"
    FETCH_ERROR = "fetch_error"
    PARSE_ERROR = "parse_error"
    GUARD_TRIPPED = "guard_tripped"


class Pilot(Base):
    __tablename__ = "pilot"

    # Telegram id, не автоинкремент.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    username: Mapped[str | None] = mapped_column(String(64))
    tg_first_name: Mapped[str | None] = mapped_column(String(128))
    tg_last_name: Mapped[str | None] = mapped_column(String(128))

    # ФИО, вытащенное из ленты календаря.
    last_name: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str | None] = mapped_column(String(128))
    middle_name: Mapped[str | None] = mapped_column(String(128))

    ics_url: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=180)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[SyncStatus | None] = mapped_column(
        Enum(SyncStatus, name="sync_status", native_enum=False, length=32)
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list[Event]] = relationship(
        back_populates="pilot",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.last_name, self.first_name, self.middle_name) if p]
        if parts:
            return " ".join(parts)
        parts = [p for p in (self.tg_first_name, self.tg_last_name) if p]
        if parts:
            return " ".join(parts)
        return f"id{self.id}"

    def __repr__(self) -> str:
        return f"<Pilot {self.id} {self.display_name}>"


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("pilot_id", "uid", name="uq_event_pilot_uid"),
        Index("ix_event_pilot_dtstart", "pilot_id", "dtstart"),
        Index("ix_event_pilot_kind_status", "pilot_id", "kind", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pilot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pilot.id", ondelete="CASCADE"), nullable=False
    )

    # UID из ленты. Строка, а не int: провайдер вправе перейти на UUID.
    uid: Mapped[str] = mapped_column(String(255), nullable=False)

    kind: Mapped[EventKind] = mapped_column(
        Enum(EventKind, name="event_kind", native_enum=False, length=32),
        default=EventKind.OTHER,
        nullable=False,
    )
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status", native_enum=False, length=32),
        default=EventStatus.SCHEDULED,
        nullable=False,
    )

    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(Text)

    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dtend: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Разобранные поля рейса. Заполняются только для kind == FLIGHT.
    flight_no: Mapped[str | None] = mapped_column(String(16))
    dep_code: Mapped[str | None] = mapped_column(String(8))
    dep_city: Mapped[str | None] = mapped_column(String(128))
    arr_code: Mapped[str | None] = mapped_column(String(8))
    arr_city: Mapped[str | None] = mapped_column(String(128))
    aircraft: Mapped[str | None] = mapped_column(String(32))

    # Ручная корректировка налёта в минутах. Если задана — идёт в зачёт
    # вместо расписания. Расписание остаётся нетронутым.
    actual_block_minutes: Mapped[int | None] = mapped_column(Integer)

    # md5 от значимых полей. Расхождение = событие изменилось.
    content_hash: Mapped[str] = mapped_column(String(32), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pilot: Mapped[Pilot] = relationship(back_populates="events")

    @property
    def scheduled_minutes(self) -> int:
        return max(0, int((self.dtend - self.dtstart).total_seconds() // 60))

    @property
    def block_minutes(self) -> int:
        """Минуты, идущие в зачёт налёта."""
        if self.actual_block_minutes is not None:
            return max(0, self.actual_block_minutes)
        return self.scheduled_minutes

    def __repr__(self) -> str:
        return f"<Event {self.uid} {self.kind.value} {self.dtstart:%d.%m %H:%M}>"
