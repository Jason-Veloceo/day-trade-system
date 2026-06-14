"""End-to-end write-through pipeline for one RawDtdEvent.

Steps:
  1. upsert symbol
  2. upsert news (if present)
  3. insert scanner_event
  4. fetch current candidate snapshot (in-memory dict cache + DB row for the symbol/day)
  5. apply event -> CandidateUpdate
  6. upsert candidates row
  7. evaluate active rule set -> FilterDecision
  8. update candidate status + failed_rules
  9. write filter_evaluations rows (replace previous evaluations for this candidate)

Returns (Candidate ORM row, CandidateSnapshot, FilterDecision) for the API/WS layer
to broadcast.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from day_trade.config import get_settings
from day_trade.db.models import (
    Candidate,
    FilterEvaluation,
    FilterRuleSet,
    News,
    ScannerEvent,
    Symbol,
)
from day_trade.db.repositories.rule_sets import get_active_rule_set
from day_trade.filters.engine import FilterDecision, evaluate
from day_trade.normalize.candidates import CandidateSnapshot, apply_event
from day_trade.normalize.scanner_events import RawDtdEvent


@dataclass(slots=True)
class PipelineResult:
    candidate_id: int
    snapshot: CandidateSnapshot
    is_new_candidate: bool
    decision: FilterDecision


async def _upsert_symbol(session: AsyncSession, symbol: str, ts: dt.datetime) -> None:
    stmt = (
        pg_insert(Symbol)
        .values(symbol=symbol, last_seen_at=ts)
        .on_conflict_do_update(
            index_elements=[Symbol.symbol], set_={"last_seen_at": ts}
        )
    )
    await session.execute(stmt)


async def _upsert_news(session: AsyncSession, event: RawDtdEvent) -> None:
    if not (event.news_id and event.news_datetime and event.news_headline):
        return
    stmt = (
        pg_insert(News)
        .values(
            newsid=event.news_id,
            symbol=event.symbol,
            datetime=event.news_datetime,
            headline=event.news_headline,
            storyurl=event.news_storyurl or "",
            raw={"headline": event.news_headline, "storyurl": event.news_storyurl},
        )
        .on_conflict_do_nothing(index_elements=[News.newsid])
    )
    await session.execute(stmt)


async def _insert_scanner_event(session: AsyncSession, event: RawDtdEvent) -> None:
    session.add(
        ScannerEvent(
            source=event.source,
            widget=event.widget,
            strategy=event.strategy,
            strategy_label=event.strategy_label,
            event=event.event,
            symbol=event.symbol,
            ts=event.ts,
            trading_day=event.trading_day,
            close_price=event.close_price,
            volume_today=event.volume_today,
            float_shares=event.float_shares,
            rel_vol_today=event.rel_vol_today,
            rel_vol_5min=event.rel_vol_5min,
            rel_gap=event.rel_gap,
            rel_gain_loss=event.rel_gain_loss,
            short_interest=event.short_interest,
            raw=event.raw,
        )
    )


def _row_to_snapshot(row: Candidate) -> CandidateSnapshot:
    return CandidateSnapshot(
        symbol=row.symbol,
        trading_day=row.trading_day,
        first_alert_at=row.first_alert_at,
        last_alert_at=row.last_alert_at,
        cooldown_until=row.cooldown_until,
        alert_count=row.alert_count,
        widgets_fired=list(row.widgets_fired or []),
        strategies_fired=list(row.strategies_fired or []),
        is_5_pillars=row.is_5_pillars,
        last_close_price=row.last_close_price,
        last_volume=row.last_volume,
        last_float=row.last_float,
        last_rel_vol_today=row.last_rel_vol_today,
        last_rel_vol_5min=row.last_rel_vol_5min,
        last_rel_gap=row.last_rel_gap,
        last_rel_gain=row.last_rel_gain,
        last_short_interest=row.last_short_interest,
        has_news=row.has_news,
        latest_newsid=row.latest_newsid,
    )


async def _fetch_active_candidate(
    session: AsyncSession, symbol: str, trading_day: dt.date, event_ts: dt.datetime
) -> Candidate | None:
    """Return the candidate row whose cooldown has not yet expired at event_ts.

    Because cooldown_until is anchored to first_alert_at, a stale event arriving
    out of order may match an older candidate - that's fine; downstream merge
    logic decides.
    """
    stmt = (
        select(Candidate)
        .where(
            Candidate.symbol == symbol,
            Candidate.trading_day == trading_day,
            Candidate.cooldown_until > event_ts,
        )
        .order_by(Candidate.first_alert_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _upsert_candidate(
    session: AsyncSession,
    existing: Candidate | None,
    snap: CandidateSnapshot,
    decision: FilterDecision,
) -> Candidate:
    if existing is None:
        row = Candidate(
            symbol=snap.symbol,
            trading_day=snap.trading_day,
            first_alert_at=snap.first_alert_at,
            last_alert_at=snap.last_alert_at,
            cooldown_until=snap.cooldown_until,
            alert_count=snap.alert_count,
            widgets_fired=list(snap.widgets_fired),
            strategies_fired=list(snap.strategies_fired),
            is_5_pillars=snap.is_5_pillars,
            last_close_price=snap.last_close_price,
            last_volume=snap.last_volume,
            last_float=snap.last_float,
            last_rel_vol_today=snap.last_rel_vol_today,
            last_rel_vol_5min=snap.last_rel_vol_5min,
            last_rel_gap=snap.last_rel_gap,
            last_rel_gain=snap.last_rel_gain,
            last_short_interest=snap.last_short_interest,
            has_news=snap.has_news,
            latest_newsid=snap.latest_newsid,
            status="passed" if decision.passed else "failed_filter",
            failed_rules=list(decision.failed_rules),
        )
        session.add(row)
        await session.flush()
        return row

    existing.last_alert_at = snap.last_alert_at
    existing.alert_count = snap.alert_count
    existing.widgets_fired = list(snap.widgets_fired)
    existing.strategies_fired = list(snap.strategies_fired)
    existing.is_5_pillars = snap.is_5_pillars
    existing.last_close_price = snap.last_close_price
    existing.last_volume = snap.last_volume
    existing.last_float = snap.last_float
    existing.last_rel_vol_today = snap.last_rel_vol_today
    existing.last_rel_vol_5min = snap.last_rel_vol_5min
    existing.last_rel_gap = snap.last_rel_gap
    existing.last_rel_gain = snap.last_rel_gain
    existing.last_short_interest = snap.last_short_interest
    existing.has_news = snap.has_news
    existing.latest_newsid = snap.latest_newsid
    existing.status = "passed" if decision.passed else "failed_filter"
    existing.failed_rules = list(decision.failed_rules)
    await session.flush()
    return existing


async def _write_evaluations(
    session: AsyncSession,
    candidate_id: int,
    rule_set: FilterRuleSet,
    decision: FilterDecision,
) -> None:
    await session.execute(
        delete(FilterEvaluation).where(FilterEvaluation.candidate_id == candidate_id)
    )
    for r in decision.results:
        session.add(
            FilterEvaluation(
                candidate_id=candidate_id,
                rule_set_id=rule_set.id,
                rule_key=r.rule_key,
                passed=r.passed,
                observed=r.observed if not isinstance(r.observed, (set, frozenset)) else list(r.observed),
                threshold=r.threshold,
            )
        )


async def ingest_event(session: AsyncSession, event: RawDtdEvent) -> PipelineResult:
    """Persist a single RawDtdEvent through the funnel."""
    settings = get_settings()
    cooldown = dt.timedelta(minutes=settings.candidate_cooldown_minutes)

    await _upsert_symbol(session, event.symbol, event.ts)
    await _upsert_news(session, event)
    await _insert_scanner_event(session, event)

    existing_row = await _fetch_active_candidate(session, event.symbol, event.trading_day, event.ts)
    existing_snap = _row_to_snapshot(existing_row) if existing_row else None

    five_pillars_id = settings.dtd_five_pillars_widget or None
    update = apply_event(existing_snap, event, cooldown=cooldown, five_pillars_widget_id=five_pillars_id)

    rule_set, rules = await get_active_rule_set(session)
    if rule_set is None:
        rules = []
    decision = evaluate(
        rules,
        update.snapshot,
        news_datetime=event.news_datetime,
        news_headline=event.news_headline,
        now=event.ts,
    )

    candidate_row = await _upsert_candidate(session, existing_row, update.snapshot, decision)

    if rule_set is not None:
        await _write_evaluations(session, candidate_row.id, rule_set, decision)

    return PipelineResult(
        candidate_id=candidate_row.id,
        snapshot=update.snapshot,
        is_new_candidate=update.is_new,
        decision=decision,
    )
