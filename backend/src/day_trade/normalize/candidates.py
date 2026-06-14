"""Per-symbol candidate rollup with cooldown.

Given a stream of RawDtdEvents, produces CandidateUpdates describing either a new
Candidate or the update of an existing one. The actual DB merge happens in the
repository - this module is pure logic so it can be unit-tested without a DB.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from day_trade.normalize.scanner_events import RawDtdEvent

FIVE_PILLARS_WIDGET_NAMES = {"5Pillars", "FivePillars", "5_Pillars"}


@dataclass(slots=True)
class CandidateSnapshot:
    """In-memory snapshot of one candidate, updated as more alerts arrive."""

    symbol: str
    trading_day: dt.date
    first_alert_at: dt.datetime
    last_alert_at: dt.datetime
    cooldown_until: dt.datetime
    alert_count: int
    widgets_fired: list[str] = field(default_factory=list)
    strategies_fired: list[str] = field(default_factory=list)
    is_5_pillars: bool = False

    last_close_price: Decimal | None = None
    last_volume: int | None = None
    last_float: int | None = None
    last_rel_vol_today: Decimal | None = None
    last_rel_vol_5min: Decimal | None = None
    last_rel_gap: Decimal | None = None
    last_rel_gain: Decimal | None = None
    last_short_interest: int | None = None

    has_news: bool = False
    latest_newsid: str | None = None


@dataclass(slots=True)
class CandidateUpdate:
    """Either a brand-new candidate or a merge into the existing one."""

    snapshot: CandidateSnapshot
    is_new: bool


def _is_five_pillars(widget: str, five_pillars_widget_id: str | None) -> bool:
    if widget in FIVE_PILLARS_WIDGET_NAMES:
        return True
    if five_pillars_widget_id and widget == five_pillars_widget_id:
        return True
    return False


def _merge_unique(existing: list[str], new_value: str) -> list[str]:
    return existing if new_value in existing else [*existing, new_value]


def apply_event(
    existing: CandidateSnapshot | None,
    event: RawDtdEvent,
    *,
    cooldown: dt.timedelta,
    five_pillars_widget_id: str | None = None,
) -> CandidateUpdate:
    """Apply a RawDtdEvent to an existing snapshot (or None) and return an update.

    Rules:
      - if no existing snapshot, or `event.ts >= existing.cooldown_until`, create a new candidate.
      - otherwise merge: bump last_alert_at, alert_count, refresh metrics, union widgets/strategies.
      - cooldown_until is anchored at first_alert_at + cooldown (not extended by later alerts).
    """
    is_5p = _is_five_pillars(event.widget, five_pillars_widget_id)

    if existing is None or event.ts >= existing.cooldown_until:
        snap = CandidateSnapshot(
            symbol=event.symbol,
            trading_day=event.trading_day,
            first_alert_at=event.ts,
            last_alert_at=event.ts,
            cooldown_until=event.ts + cooldown,
            alert_count=1,
            widgets_fired=[event.widget],
            strategies_fired=[event.strategy],
            is_5_pillars=is_5p,
            last_close_price=event.close_price,
            last_volume=event.volume_today,
            last_float=event.float_shares,
            last_rel_vol_today=event.rel_vol_today,
            last_rel_vol_5min=event.rel_vol_5min,
            last_rel_gap=event.rel_gap,
            last_rel_gain=event.rel_gain_loss,
            last_short_interest=event.short_interest,
            has_news=event.news_id is not None,
            latest_newsid=event.news_id,
        )
        return CandidateUpdate(snapshot=snap, is_new=True)

    merged = CandidateSnapshot(
        symbol=existing.symbol,
        trading_day=existing.trading_day,
        first_alert_at=existing.first_alert_at,
        last_alert_at=max(existing.last_alert_at, event.ts),
        cooldown_until=existing.cooldown_until,
        alert_count=existing.alert_count + 1,
        widgets_fired=_merge_unique(existing.widgets_fired, event.widget),
        strategies_fired=_merge_unique(existing.strategies_fired, event.strategy),
        is_5_pillars=existing.is_5_pillars or is_5p,
        last_close_price=event.close_price if event.close_price is not None else existing.last_close_price,
        last_volume=event.volume_today if event.volume_today is not None else existing.last_volume,
        last_float=event.float_shares if event.float_shares is not None else existing.last_float,
        last_rel_vol_today=(
            event.rel_vol_today if event.rel_vol_today is not None else existing.last_rel_vol_today
        ),
        last_rel_vol_5min=(
            event.rel_vol_5min if event.rel_vol_5min is not None else existing.last_rel_vol_5min
        ),
        last_rel_gap=event.rel_gap if event.rel_gap is not None else existing.last_rel_gap,
        last_rel_gain=event.rel_gain_loss if event.rel_gain_loss is not None else existing.last_rel_gain,
        last_short_interest=(
            event.short_interest if event.short_interest is not None else existing.last_short_interest
        ),
        has_news=existing.has_news or event.news_id is not None,
        latest_newsid=event.news_id or existing.latest_newsid,
    )
    return CandidateUpdate(snapshot=merged, is_new=False)
