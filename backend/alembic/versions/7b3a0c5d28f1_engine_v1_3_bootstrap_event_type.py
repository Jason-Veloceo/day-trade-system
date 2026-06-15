"""engine v1.3: bootstrap event type

Revision ID: 7b3a0c5d28f1
Revises: 4e1f0a82c9b1
Create Date: 2026-06-15 22:40:00.000000

Adds:
  - engine_event_type value: 'bootstrap'

Recorded by the engine when historical 1m bars are replayed at engine
start to warm up MACD / VWAP / pullback history before going live.
Payload includes counts (bars_1m, bars_5m_emitted), the time range
covered, and the post-bootstrap indicator snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7b3a0c5d28f1"
down_revision: str | None = "4e1f0a82c9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ALTER TYPE ADD VALUE inside a transaction; the
    # value cannot be referenced in the same transaction but the engine
    # writes 'bootstrap' rows at runtime in a separate transaction, so this
    # is safe. IF NOT EXISTS makes the migration idempotent.
    op.execute("ALTER TYPE engine_event_type ADD VALUE IF NOT EXISTS 'bootstrap'")


def downgrade() -> None:
    # PostgreSQL has no ALTER TYPE DROP VALUE; an enum value can only be
    # removed by recreating the type and rewriting every column that
    # references it. The new value is harmless, so downgrade is a no-op.
    pass
