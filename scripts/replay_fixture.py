"""Replay a captured DTD `/alert?widget=...` JSON fixture through the live pipeline.

Examples:
    # Fast (no pacing) - good for backfilling history into a fresh DB:
    uv run python ../scripts/replay_fixture.py --fast

    # Real-time-ish at 60x: 1 minute of session in 1 second of wall time
    uv run python ../scripts/replay_fixture.py --speed 60

    # Limit to first 200 events for a quick smoke
    uv run python ../scripts/replay_fixture.py --fast --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from day_trade.ingest.dtd.replay import replay_fast, replay_timed  # noqa: E402

DEFAULT_FIXTURE = (
    REPO_ROOT / "backend" / "src" / "day_trade" / "ingest" / "dtd" / "fixtures" / "momo_response.json"
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Path to DTD alert JSON fixture")
    parser.add_argument("--fast", action="store_true", help="Process as fast as possible (no pacing)")
    parser.add_argument("--speed", type=float, default=60.0, help="Replay-time multiplier for timed mode")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N events")
    args = parser.parse_args()

    path = Path(args.fixture)
    if not path.exists():
        raise SystemExit(f"Fixture not found: {path}")

    if args.fast:
        count = asyncio.run(replay_fast(path, limit=args.limit))
    else:
        count = asyncio.run(replay_timed(path, speed=args.speed, limit=args.limit))

    print(f"Processed {count} events")


if __name__ == "__main__":
    main()
