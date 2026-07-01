"""REST API for the auto-arm worker.

  GET  /auto_arm/status   -> snapshot of the worker + current config
  POST /auto_arm/enable   -> runtime override to enable the worker
  POST /auto_arm/disable  -> runtime override to disable the worker

The runtime override mutates `Settings.auto_arm_enabled` on the
in-memory settings object so the next poll-tick observes the change.
It does NOT touch the .env file — to make the change survive a process
restart the operator should also update .env. The endpoint returns
the live snapshot after applying the change so the UI can render the
new state immediately.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from day_trade.auto_arm import get_worker
from day_trade.config import get_settings

router = APIRouter(prefix="/auto_arm", tags=["auto_arm"])


class AutoArmStatusOut(BaseModel):
    enabled: bool
    widgets: list[str]
    window_et: str
    max_per_day: int
    max_per_hour: int
    rearm_cooldown_minutes: int
    stale_after_minutes: int
    poll_seconds: float
    last_tick_at: str | None
    last_tick_decisions: int
    running: bool


class AutoArmConfigPatch(BaseModel):
    """Runtime overrides for auto-arm config. Only whitelisted fields
    are patchable in-process; everything else requires a .env edit + full
    backend restart. Fields left as None are unchanged."""

    widgets: list[str] | None = Field(
        default=None,
        description="Scanner widget names to arm on. Must be non-empty if provided.",
    )
    autonomous: bool | None = Field(
        default=None,
        description=(
            "If True, future auto-armed engines skip the manual-approval gate and "
            "execute signals directly (paper only per PAPER_TRADING_ONLY). Does NOT "
            "affect already-armed engines — use /engine/runs/{id}/autonomous for those."
        ),
    )
    max_per_hour: int | None = Field(
        default=None, ge=0,
        description="Cap on auto-arms per rolling hour. 0 = disabled (unlimited).",
    )
    max_per_day: int | None = Field(
        default=None, ge=0,
        description="Cap on auto-arms per session day. 0 = disabled (unlimited).",
    )
    rearm_cooldown_minutes: int | None = Field(
        default=None, ge=0,
        description="Per-symbol cooldown after stop before re-arm is allowed. 0 = disabled.",
    )


def _snapshot() -> dict[str, Any]:
    return get_worker().snapshot()


@router.get("/status", response_model=AutoArmStatusOut)
async def status() -> AutoArmStatusOut:
    return AutoArmStatusOut(**_snapshot())


@router.post("/enable", response_model=AutoArmStatusOut)
async def enable() -> AutoArmStatusOut:
    """Runtime override: enable the worker even if .env says disabled.
    Persists for the lifetime of the process only."""
    get_settings().auto_arm_enabled = True
    return AutoArmStatusOut(**_snapshot())


@router.post("/disable", response_model=AutoArmStatusOut)
async def disable() -> AutoArmStatusOut:
    """Runtime override: disable the worker. Existing engines that
    were auto-armed earlier are NOT affected (they keep running);
    only new arm attempts stop firing. Staleness still applies."""
    get_settings().auto_arm_enabled = False
    return AutoArmStatusOut(**_snapshot())


@router.patch("/config", response_model=AutoArmStatusOut)
async def patch_config(patch: AutoArmConfigPatch) -> AutoArmStatusOut:
    """Runtime override for the widget list (and future patchable fields).

    In-process only — the next backend restart re-reads .env, so persistent
    changes still belong there. The worker re-reads settings every tick, so
    the new widget list is honoured on the next poll.
    """
    settings = get_settings()
    if patch.widgets is not None:
        cleaned = [w.strip() for w in patch.widgets if w.strip()]
        if not cleaned:
            raise HTTPException(
                status_code=400, detail="widgets must contain at least one entry"
            )
        settings.auto_arm_widgets = ",".join(cleaned)
    if patch.autonomous is not None:
        settings.auto_arm_autonomous = patch.autonomous
    if patch.max_per_hour is not None:
        settings.auto_arm_max_per_hour = patch.max_per_hour
    if patch.max_per_day is not None:
        settings.auto_arm_max_per_day = patch.max_per_day
    if patch.rearm_cooldown_minutes is not None:
        settings.auto_arm_rearm_cooldown_minutes = patch.rearm_cooldown_minutes
    return AutoArmStatusOut(**_snapshot())
