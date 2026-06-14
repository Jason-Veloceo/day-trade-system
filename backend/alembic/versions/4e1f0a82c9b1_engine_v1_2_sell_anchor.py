"""engine v1.2: sell_anchor column on engine_runs

Revision ID: 4e1f0a82c9b1
Revises: 2a3c91d70a44
Create Date: 2026-06-14 08:15:00.000000

Adds:
  - engine_runs.sell_anchor (TEXT, NOT NULL, default 'bid')

Records the SELL leg routing choice for each run:
  'bid' -> LMT @ bid - offset (aggressive, default; matches "Sell ... at Bid" hotkeys)
  'ask' -> LMT @ ask - offset (passive,        matches "Sell ... at Ask"  hotkeys)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e1f0a82c9b1"
down_revision: str | None = "2a3c91d70a44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "engine_runs",
        sa.Column("sell_anchor", sa.Text(), server_default="bid", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("engine_runs", "sell_anchor")
