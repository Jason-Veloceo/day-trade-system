"""engine v1.1: dtd_context + LMT routing + L2/T&S enable + new event types

Revision ID: 2a3c91d70a44
Revises: 1f588b56d403
Create Date: 2026-06-14 04:50:00.000000

Adds:
  - engine_runs.dtd_context  (JSONB, NOT NULL, default '{}')
  - engine_runs.order_type   (TEXT, NOT NULL, default 'MKT')
  - engine_runs.limit_offset_cents (NUMERIC(8,2), NOT NULL, default 10)
  - engine_runs.enable_depth (BOOLEAN, NOT NULL, default false)
  - engine_runs.enable_tape  (BOOLEAN, NOT NULL, default false)
  - engine_event_type values: depth_update, tape_print, exit_trigger,
    feature_snapshot
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2a3c91d70a44"
down_revision: str | None = "1f588b56d403"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_EVENT_TYPES = (
    "depth_update",
    "tape_print",
    "exit_trigger",
    "feature_snapshot",
)


def upgrade() -> None:
    # ---- engine_runs new columns ----
    op.add_column(
        "engine_runs",
        sa.Column(
            "dtd_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "engine_runs",
        sa.Column("order_type", sa.Text(), server_default="MKT", nullable=False),
    )
    op.add_column(
        "engine_runs",
        sa.Column(
            "limit_offset_cents",
            sa.Numeric(precision=8, scale=2),
            server_default="10",
            nullable=False,
        ),
    )
    op.add_column(
        "engine_runs",
        sa.Column("enable_depth", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "engine_runs",
        sa.Column("enable_tape", sa.Boolean(), server_default="false", nullable=False),
    )

    # ---- engine_event_type enum: add new values ----
    # PostgreSQL 12+ allows ALTER TYPE ADD VALUE inside a transaction, BUT
    # the newly added value cannot be referenced in the SAME transaction.
    # That's fine here because we don't write any rows with the new values
    # in this migration - the engine writes them at runtime, in a separate
    # transaction. We use IF NOT EXISTS for idempotency.
    for value in _NEW_EVENT_TYPES:
        op.execute(f"ALTER TYPE engine_event_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Drop the new engine_runs columns.
    op.drop_column("engine_runs", "enable_tape")
    op.drop_column("engine_runs", "enable_depth")
    op.drop_column("engine_runs", "limit_offset_cents")
    op.drop_column("engine_runs", "order_type")
    op.drop_column("engine_runs", "dtd_context")

    # Removing enum values is intentionally not supported (PostgreSQL doesn't
    # provide a clean way to remove an enum value without recreating the type
    # and re-mapping every row that uses it). Downgrade leaves the values in
    # place; they're harmless.
