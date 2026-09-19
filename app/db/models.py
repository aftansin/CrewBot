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
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
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


# ==========================================================================
# Лётная книжка
# ==========================================================================
#
# Привязка к пилоту: справочники бортов и аэропортов общие — Boeing 737
# и Шереметьево одинаковы для всех. Люди, работодатели и рейсы у каждого
# пилота свои: в карточках людей лежат личные заметки и фото.

BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Function(str, enum.Enum):
    """Функция пилота по AMC1 FCL.050."""
    PIC = "pic"
    PICUS = "picus"
    COPILOT = "copilot"
    CRUISE_RELIEF = "cruise_relief"
    DUAL = "dual"
    FI = "fi"
    FE = "fe"
    UNVERIFIED = "unverified"


class CrewRole(str, enum.Enum):
    PIC = "PIC"
    SIC = "SIC"
    RELIEF = "RELIEF"
    RELIEF2 = "RELIEF2"
    INSTRUCTOR = "INSTRUCTOR"
    STUDENT = "STUDENT"
    OBSERVER = "OBSERVER"
    CABIN = "CABIN"


class FlightSource(str, enum.Enum):
    LOGTEN = "logten"        # перенесено из LogTen Pro
    CALENDAR = "calendar"    # заведено из расписания
    MANUAL = "manual"        # заведено вручную


class Airport(Base):
    __tablename__ = "airport"

    icao: Mapped[str] = mapped_column(String(4), primary_key=True)
    iata: Mapped[str | None] = mapped_column(String(3))
    name: Mapped[str | None] = mapped_column(String(160))
    municipality: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def coords(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return self.latitude, self.longitude


class Aircraft(Base):
    """Две регистрации — один самолёт."""
    __tablename__ = "aircraft"
    __table_args__ = (
        UniqueConstraint("registration", name="uq_aircraft_registration"),
        Index("ix_aircraft_ra", "registration_ra"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration: Mapped[str] = mapped_column(String(16), nullable=False)
    registration_ra: Mapped[str | None] = mapped_column(String(16))
    type_code: Mapped[str | None] = mapped_column(String(16))
    type_name: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    year_built: Mapped[int | None] = mapped_column(Integer)
    multi_pilot: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)

    @property
    def display(self) -> str:
        return self.registration_ra or self.registration


class Employer(Base):
    __tablename__ = "employer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pilot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pilot.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)

    def covers(self, day: date) -> bool:
        return self.started_on <= day and (self.ended_on is None or day <= self.ended_on)


class Person(Base):
    __tablename__ = "person"
    __table_args__ = (Index("ix_person_pilot", "pilot_id", "last_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pilot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pilot.id", ondelete="CASCADE"), nullable=False
    )
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(64))
    middle_name: Mapped[str | None] = mapped_column(String(64))
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)

    aliases: Mapped[list[PersonAlias]] = relationship(
        back_populates="person", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def display(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)


class PersonAlias(Base):
    __tablename__ = "person_alias"
    __table_args__ = (UniqueConstraint("person_id", "alias", name="uq_person_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    person: Mapped[Person] = relationship(back_populates="aliases")


class Flight(Base):
    __tablename__ = "flight"
    __table_args__ = (
        Index("ix_flight_pilot_date", "pilot_id", "flight_date"),
        Index("ix_flight_pilot_route", "pilot_id", "dep_icao", "arr_icao"),
        Index("ix_flight_aircraft", "aircraft_id"),
        UniqueConstraint("pilot_id", "import_key", name="uq_flight_import_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    pilot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pilot.id", ondelete="CASCADE"), nullable=False
    )

    flight_date: Mapped[date] = mapped_column(Date, nullable=False)
    out_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    in_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    dep_icao: Mapped[str | None] = mapped_column(String(4))
    arr_icao: Mapped[str | None] = mapped_column(String(4))
    aircraft_id: Mapped[int | None] = mapped_column(ForeignKey("aircraft.id"))
    employer_id: Mapped[int | None] = mapped_column(ForeignKey("employer.id"))
    flight_number: Mapped[str | None] = mapped_column(String(16))

    block_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    function: Mapped[Function] = mapped_column(
        Enum(Function, name="flight_function", native_enum=False, length=20),
        default=Function.UNVERIFIED, nullable=False,
    )
    function_source: Mapped[str | None] = mapped_column(String(64))
    pilot_flying: Mapped[bool | None] = mapped_column(Boolean)

    night_minutes: Mapped[int] = mapped_column(Integer, default=0)
    night_computed: Mapped[bool] = mapped_column(Boolean, default=False)
    ifr_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cross_country_minutes: Mapped[int] = mapped_column(Integer, default=0)
    dual_received_minutes: Mapped[int] = mapped_column(Integer, default=0)
    dual_given_minutes: Mapped[int] = mapped_column(Integer, default=0)

    day_takeoffs: Mapped[int] = mapped_column(Integer, default=0)
    night_takeoffs: Mapped[int] = mapped_column(Integer, default=0)
    day_landings: Mapped[int] = mapped_column(Integer, default=0)
    night_landings: Mapped[int] = mapped_column(Integer, default=0)

    on_duty_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    off_duty_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duty_minutes: Mapped[int | None] = mapped_column(Integer)
    duty_computed: Mapped[bool] = mapped_column(Boolean, default=False)

    distance_nm: Mapped[float | None] = mapped_column(Float)
    remarks: Mapped[str | None] = mapped_column(Text)

    source: Mapped[FlightSource] = mapped_column(
        Enum(FlightSource, name="flight_source", native_enum=False, length=16),
        default=FlightSource.MANUAL, nullable=False,
    )
    # Связь с расписанием — справочная. Запись никогда не меняется вслед
    # за событием: план правится задним числом, книжка фиксирует факт.
    source_event_uid: Mapped[str | None] = mapped_column(String(255))
    # Снимок плана на момент создания. По нему обнаруживается поздняя правка.
    plan_snapshot: Mapped[dict | None] = mapped_column(JSON)
    # Пилот увидел расхождение и принял решение.
    divergence_ack: Mapped[bool] = mapped_column(Boolean, default=False)

    import_key: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    aircraft: Mapped[Aircraft | None] = relationship(lazy="selectin")
    employer: Mapped[Employer | None] = relationship(lazy="selectin")
    crew: Mapped[list[FlightCrew]] = relationship(
        back_populates="flight", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def easa_pic_minutes(self) -> int:
        if self.function in (Function.PIC, Function.PICUS, Function.FI, Function.FE):
            return self.block_minutes
        return 0

    @property
    def easa_copilot_minutes(self) -> int:
        if self.function in (Function.COPILOT, Function.CRUISE_RELIEF):
            return self.block_minutes
        return 0

    @property
    def easa_dual_minutes(self) -> int:
        return self.block_minutes if self.function is Function.DUAL else 0

    @property
    def faa_pic_minutes(self) -> int:
        if self.easa_pic_minutes:
            return self.block_minutes
        if self.function is Function.COPILOT and self.pilot_flying:
            return self.block_minutes
        return 0


class FlightCrew(Base):
    __tablename__ = "flight_crew"
    __table_args__ = (
        UniqueConstraint("flight_id", "person_id", "role", name="uq_flight_crew"),
        Index("ix_flight_crew_person", "person_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flight.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[CrewRole] = mapped_column(
        Enum(CrewRole, name="crew_role", native_enum=False, length=16), nullable=False
    )
    pilot_flying: Mapped[bool] = mapped_column(Boolean, default=False)

    flight: Mapped[Flight] = relationship(back_populates="crew")
    person: Mapped[Person] = relationship(lazy="selectin")
