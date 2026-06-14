"""One-shot DB init. Run after `alembic upgrade head`.

- Ensures the `daytrade` database exists (must be created beforehand via psql -
  this script does NOT bootstrap the database itself, per the rule against creating
  resources without the user's go).
- Seeds the default Stage-1 filter rule set if no active set exists.

Usage:
    cd backend && uv run python ../scripts/init_db.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from day_trade.config import get_settings  # noqa: E402
from day_trade.db.repositories.rule_sets import ensure_default_rule_set  # noqa: E402
from day_trade.db.session import session_scope  # noqa: E402
from day_trade.filters.defaults import default_rules  # noqa: E402


async def main() -> None:
    settings = get_settings()
    async for session in session_scope():
        rule_set = await ensure_default_rule_set(
            session, name="default", rules=default_rules(settings)
        )
        print(f"Active rule set: id={rule_set.id} name={rule_set.name!r}")


if __name__ == "__main__":
    asyncio.run(main())
