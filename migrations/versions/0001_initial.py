"""Начальная схема

Revision ID: 0001
Revises:
Create Date: 2026-09-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_KIND = sa.Enum(
    "FLIGHT", "SIMULATOR", "TRAINING", "MEDICAL", "REPORTING", "STANDBY", "DUTY", "REST", "OTHER",
    name="event_kind",
    native_enum=False,
    length=32,
)
EVENT_STATUS = sa.Enum(
    "SCHEDULED", "CANCELLED", name="event_status", native_enum=False, length=32
)
SYNC_STATUS = sa.Enum(
    "OK", "FETCH_ERROR", "PARSE_ERROR", "GUARD_TRIPPED",
    name="sync_status",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    op.create_table(
        "pilot",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("tg_first_name", sa.String(length=128), nullable=True),
        sa.Column("tg_last_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("middle_name", sa.String(length=128), nullable=True),
        sa.Column("ics_url", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Europe/Moscow"),
        sa.Column("poll_interval_minutes", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", SYNC_STATUS, nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pilot_id", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.String(length=255), nullable=False),
        sa.Column("kind", EVENT_KIND, nullable=False, server_default="OTHER"),
        sa.Column("status", EVENT_STATUS, nullable=False, server_default="SCHEDULED"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("dtstart", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dtend", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flight_no", sa.String(length=16), nullable=True),
        sa.Column("dep_code", sa.String(length=8), nullable=True),
        sa.Column("dep_city", sa.String(length=128), nullable=True),
        sa.Column("arr_code", sa.String(length=8), nullable=True),
        sa.Column("arr_city", sa.String(length=128), nullable=True),
        sa.Column("aircraft", sa.String(length=32), nullable=True),
        sa.Column("actual_block_minutes", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["pilot_id"], ["pilot.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Уникальность по паре, а не глобально по UID: один рейс у двух
        # пилотов больше не конфликтует.
        sa.UniqueConstraint("pilot_id", "uid", name="uq_event_pilot_uid"),
    )
    op.create_index("ix_event_pilot_dtstart", "event", ["pilot_id", "dtstart"])
    op.create_index("ix_event_pilot_kind_status", "event", ["pilot_id", "kind", "status"])


def downgrade() -> None:
    op.drop_index("ix_event_pilot_kind_status", table_name="event")
    op.drop_index("ix_event_pilot_dtstart", table_name="event")
    op.drop_table("event")
    op.drop_table("pilot")
