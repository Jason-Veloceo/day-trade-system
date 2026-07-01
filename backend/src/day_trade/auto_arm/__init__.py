"""Auto-arm subsystem.

Polls the candidates table for newly-passed scanner alerts, applies a
gate stack (widget filter, status, time window, rate limits, dedupe,
portfolio caps), and calls the engine registry to start a new engine
when all gates pass. Also runs a staleness watcher that auto-stops
auto-armed engines whose underlying scanner alert has gone cold (no
new alerts within `auto_arm_stale_after_minutes`) and which are not
currently holding a position.

Public surface:

  - AutoArmWorker.start() / .stop() — lifecycle hooks called from the
    FastAPI lifespan.
  - decide(candidate, ctx) — pure decision function (tested in
    isolation; no DB or registry coupling).
  - is_engine_stale(engine, ctx) — pure staleness decision (same).
"""

from day_trade.auto_arm.policy import (
    AutoArmConfig,
    Decision,
    EngineStatusView,
    PolicyContext,
    RecentArm,
    StalenessDecision,
    decide,
    is_engine_stale,
    parse_window,
)
from day_trade.auto_arm.worker import AutoArmWorker, get_worker

__all__ = [
    "AutoArmConfig",
    "AutoArmWorker",
    "Decision",
    "EngineStatusView",
    "PolicyContext",
    "RecentArm",
    "StalenessDecision",
    "decide",
    "get_worker",
    "is_engine_stale",
    "parse_window",
]
