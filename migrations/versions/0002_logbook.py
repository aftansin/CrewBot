"""Таблицы лётной книжки

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = sa.Enum("PIC", "PICUS", "COPILOT", "CRUISE_RELIEF", "DUAL", "FI", "FE",
                   "UNVERIFIED", name="flight_function", native_enum=False, length=20)
CREW_ROLE = sa.Enum("PIC", "SIC", "RELIEF", "RELIEF2", "INSTRUCTOR", "STUDENT",
                    "OBSERVER", "CABIN", name="crew_role", native_enum=False, length=16)
SOURCE = sa.Enum("LOGTEN", "CALENDAR", "MANUAL", name="flight_source",
                 native_enum=False, length=16)


def upgrade() -> None:
    op.create_table(
        "airport",
        sa.Column("icao", sa.String(length=4), nullable=False),
        sa.Column("iata", sa.String(length=3), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=True),
        sa.Column("municipality", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("icao"),
    )
    op.create_index("ix_airport_iata", "airport", ["iata"])

    op.create_table(
        "aircraft",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("registration", sa.String(length=16), nullable=False),
        sa.Column("registration_ra", sa.String(length=16), nullable=True),
        sa.Column("type_code", sa.String(length=16), nullable=True),
        sa.Column("type_name", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("year_built", sa.Integer(), nullable=True),
        sa.Column("multi_pilot", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("registration", name="uq_aircraft_registration"),
    )
    op.create_index("ix_aircraft_ra", "aircraft", ["registration_ra"])

    op.create_table(
        "employer",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pilot_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pilot_id"], ["pilot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "person",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pilot_id", sa.BigInteger(), nullable=False),
        sa.Column("last_name", sa.String(length=64), nullable=False),
        sa.Column("first_name", sa.String(length=64), nullable=True),
        sa.Column("middle_name", sa.String(length=64), nullable=True),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("photo_file_id", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pilot_id"], ["pilot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_person_pilot", "person", ["pilot_id", "last_name"])

    op.create_table(
        "person_alias",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=160), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "alias", name="uq_person_alias"),
    )

    op.create_table(
        "flight",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pilot_id", sa.BigInteger(), nullable=False),
        sa.Column("flight_date", sa.Date(), nullable=False),
        sa.Column("out_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dep_icao", sa.String(length=4), nullable=True),
        sa.Column("arr_icao", sa.String(length=4), nullable=True),
        sa.Column("aircraft_id", sa.Integer(), nullable=True),
        sa.Column("employer_id", sa.Integer(), nullable=True),
        sa.Column("flight_number", sa.String(length=16), nullable=True),
        sa.Column("block_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("function", FUNCTION, nullable=False, server_default="UNVERIFIED"),
        sa.Column("function_source", sa.String(length=64), nullable=True),
        sa.Column("pilot_flying", sa.Boolean(), nullable=True),
        sa.Column("night_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("night_computed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ifr_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cross_country_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dual_received_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dual_given_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_takeoffs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("night_takeoffs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_landings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("night_landings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("on_duty_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("off_duty_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duty_minutes", sa.Integer(), nullable=True),
        sa.Column("duty_computed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("distance_nm", sa.Float(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("source", SOURCE, nullable=False, server_default="MANUAL"),
        sa.Column("source_event_uid", sa.String(length=255), nullable=True),
        sa.Column("plan_snapshot", sa.JSON(), nullable=True),
        sa.Column("divergence_ack", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("import_key", sa.String(length=128), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["pilot_id"], ["pilot.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["aircraft_id"], ["aircraft.id"]),
        sa.ForeignKeyConstraint(["employer_id"], ["employer.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pilot_id", "import_key", name="uq_flight_import_key"),
    )
    op.create_index("ix_flight_pilot_date", "flight", ["pilot_id", "flight_date"])
    op.create_index("ix_flight_pilot_route", "flight", ["pilot_id", "dep_icao", "arr_icao"])
    op.create_index("ix_flight_aircraft", "flight", ["aircraft_id"])

    op.create_table(
        "flight_crew",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("flight_id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("role", CREW_ROLE, nullable=False),
        sa.Column("pilot_flying", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["flight_id"], ["flight.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flight_id", "person_id", "role", name="uq_flight_crew"),
    )
    op.create_index("ix_flight_crew_person", "flight_crew", ["person_id"])


def downgrade() -> None:
    op.drop_table("flight_crew")
    op.drop_table("flight")
    op.drop_table("person_alias")
    op.drop_table("person")
    op.drop_table("employer")
    op.drop_table("aircraft")
    op.drop_table("airport")
