"""SQLAlchemy 2.0 typed models matching the schema declared in the plan.

Stored types follow the plan verbatim. JSONB used for `raw` payloads and rule values
so we can iterate on rule semantics without migrations.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

# ----- enums -----

candidate_status_enum = Enum(
    "passed",
    "failed_filter",
    "stale",
    name="candidate_status",
    create_type=True,
)

rule_op_enum = Enum(
    "lt",
    "le",
    "gt",
    "ge",
    "eq",
    "between",
    "in",
    "contains_any",
    "contains_none",
    "within_minutes",
    name="rule_op",
    create_type=True,
)

trade_plan_status_enum = Enum(
    "draft",
    "submitted",
    "filled",
    "closed",
    "cancelled",
    name="trade_plan_status",
    create_type=True,
)

engine_run_status_enum = Enum(
    "starting",
    "running",
    "stopping",
    "stopped",
    "error",
    name="engine_run_status",
    create_type=True,
)

engine_event_type_enum = Enum(
    "bar",
    "indicator",
    "signal",
    "decision",
    "risk_block",
    "ready_for_approval",
    "approval_granted",
    "approval_rejected",
    "order_submit",
    "order_status",
    "fill",
    "position_open",
    "position_close",
    "slippage",
    "error",
    "engine_start",
    "engine_stop",
    "ibkr_connected",
    "ibkr_disconnected",
    "depth_update",
    "tape_print",
    "exit_trigger",
    "feature_snapshot",
    name="engine_event_type",
    create_type=True,
)


# ----- core tables -----


class Symbol(Base):
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    exchange: Mapped[str] = mapped_column(Text, nullable=False, default="NASDAQ", server_default="NASDAQ")
    listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScannerEvent(Base):
    """One row per fired DTD alert. Dedup happens at the candidate level, not here -
    here we keep the full history for replay and post-mortem."""

    __tablename__ = "scanner_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    widget: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_label: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trading_day: Mapped[dt.date] = mapped_column(Date, nullable=False)

    close_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    volume_today: Mapped[int | None] = mapped_column(BigInteger)
    float_shares: Mapped[int | None] = mapped_column(BigInteger)
    # Relative volume can spike to huge multiples when the baseline is near zero
    # (a stock that normally trades 100 shares/min suddenly trading 10M).
    rel_vol_today: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rel_vol_5min: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rel_gap: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    rel_gain_loss: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    short_interest: Mapped[int | None] = mapped_column(BigInteger)

    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("idx_scanner_events_symbol_ts", "symbol", "ts"),
        Index("idx_scanner_events_strategy_ts", "strategy", "ts"),
        Index("idx_scanner_events_day", "trading_day"),
    )


class News(Base):
    """DTD-joined news. We dedupe by newsid; a single newsid may map to multiple
    scanner events but we only store the news row once."""

    __tablename__ = "news"

    newsid: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    datetime_: Mapped[dt.datetime] = mapped_column("datetime", DateTime(timezone=True), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    storyurl: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("idx_news_symbol_datetime", "symbol", "datetime"),)


class Candidate(Base):
    """Per-symbol candidate. One row per symbol per cooldown window.

    A new alert for a symbol that has no active candidate (cooldown_until in the
    past) creates a new row. An alert inside the window updates the existing row
    (merging strategies, refreshing metrics, bumping last_alert_at).
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    trading_day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    first_alert_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_alert_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cooldown_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    widgets_fired: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    strategies_fired: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    is_5_pillars: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    last_close_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    last_volume: Mapped[int | None] = mapped_column(BigInteger)
    last_float: Mapped[int | None] = mapped_column(BigInteger)
    last_rel_vol_today: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    last_rel_vol_5min: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    last_rel_gap: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    last_rel_gain: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    last_short_interest: Mapped[int | None] = mapped_column(BigInteger)

    has_news: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    latest_newsid: Mapped[str | None] = mapped_column(ForeignKey("news.newsid"))

    status: Mapped[str] = mapped_column(candidate_status_enum, nullable=False, default="passed", server_default="passed")
    failed_rules: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default="{}"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    evaluations: Mapped[list[FilterEvaluation]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_candidates_symbol_day", "symbol", "trading_day"),
        Index("idx_candidates_day_status", "trading_day", "status"),
        Index("idx_candidates_cooldown", "cooldown_until"),
    )


# ----- filter rules -----


class FilterRuleSet(Base):
    """Versioned named bundle of filter rules. Exactly one row is `is_active`."""

    __tablename__ = "filter_rule_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text)

    rules: Mapped[list[FilterRule]] = relationship(
        back_populates="rule_set", cascade="all, delete-orphan", order_by="FilterRule.id"
    )

    __table_args__ = (
        Index("idx_filter_rule_sets_one_active", "is_active", unique=True, postgresql_where=is_active.is_(True)),
    )


class FilterRule(Base):
    __tablename__ = "filter_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_set_id: Mapped[int] = mapped_column(
        ForeignKey("filter_rule_sets.id", ondelete="CASCADE"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    op: Mapped[str] = mapped_column(rule_op_enum, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="hard", server_default="hard")
    note: Mapped[str | None] = mapped_column(Text)

    rule_set: Mapped[FilterRuleSet] = relationship(back_populates="rules")

    __table_args__ = (
        CheckConstraint("severity IN ('hard','soft')", name="ck_filter_rules_severity"),
        Index("idx_filter_rules_set", "rule_set_id"),
    )


class FilterEvaluation(Base):
    """Record of one rule evaluation against one candidate. Lets the UI explain
    exactly why a candidate passed or failed."""

    __tablename__ = "filter_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("filter_rule_sets.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[Any] = mapped_column(JSONB, nullable=False)
    threshold: Mapped[Any] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candidate: Mapped[Candidate] = relationship(back_populates="evaluations")

    __table_args__ = (Index("idx_filter_evaluations_candidate", "candidate_id"),)


# ----- trade plans / orders / fills -----


class TradePlan(Base):
    __tablename__ = "trade_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    position_size: Mapped[int] = mapped_column(Integer, nullable=False)
    max_risk_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        trade_plan_status_enum, nullable=False, default="draft", server_default="draft"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ibkr_order_id: Mapped[int | None] = mapped_column(BigInteger)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("side IN ('long','short')", name="ck_trade_plans_side"),)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"))
    engine_run_id: Mapped[int | None] = mapped_column(ForeignKey("engine_runs.id"))
    ibkr_order_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    engine_run_id: Mapped[int | None] = mapped_column(ForeignKey("engine_runs.id"))
    symbol: Mapped[str] = mapped_column(ForeignKey("symbols.symbol"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Slippage attribution: which signal triggered this fill, and how far did
    # the fill deviate from the price observed when the signal fired. NULL when
    # the fill is not tied to an engine-originated signal (e.g. manual close).
    signal_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    signal_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    slippage_cents: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    latency_ms: Mapped[int | None] = mapped_column(Integer)


# ----- risk + session -----


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SessionState(Base):
    __tablename__ = "session_state"

    trading_day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    trades_taken: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    kill_switch_on: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ----- engine (POC trading engine) -----


class EngineRun(Base):
    """One row per engine session. An engine session is the user clicking
    "Start" on the /engine page until they click "Stop" (or an error stops it).
    """

    __tablename__ = "engine_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[str] = mapped_column(Text, nullable=False, default="stock")
    strategy_name: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_caps: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    autonomous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    market_data_type: Mapped[str] = mapped_column(Text, nullable=False)
    ibkr_client_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ibkr_account: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        engine_run_status_enum, nullable=False, default="starting", server_default="starting"
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(Text)

    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    trades_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # ----- v1.1 fields for the semi-automated FirstPullback workflow -----
    # DTD-derived (or user-typed) context the trader was looking at when they
    # armed the symbol. Pure data dump - alert type, gap %, float, news, etc.
    dtd_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Order routing config for this run.
    order_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="MKT", server_default="MKT"
    )
    limit_offset_cents: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("10"), server_default="10"
    )
    # 'bid' (aggressive, default) or 'ask' (passive). Applies to SELL legs only.
    sell_anchor: Mapped[str] = mapped_column(
        Text, nullable=False, default="bid", server_default="bid"
    )
    enable_depth: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    enable_tape: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    events: Mapped[list[EngineEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_engine_runs_status", "status"),
        Index("idx_engine_runs_started_at", "started_at"),
    )


class EngineEvent(Base):
    """Append-only audit log of everything the engine did. Drives the live event
    log on the UI and the post-mortem journal/replay."""

    __tablename__ = "engine_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("engine_runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    event_type: Mapped[str] = mapped_column(engine_event_type_enum, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    run: Mapped[EngineRun] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_engine_events_run_ts", "run_id", "ts"),
        Index("idx_engine_events_type", "event_type"),
    )


class BarAggregate(Base):
    """Persisted 1m OHLCV bars per engine run. We keep these for the chart in
    the UI and to support post-run analysis without re-querying IBKR."""

    __tablename__ = "bar_aggregates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("engine_runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=Decimal("0"))

    # MACD values computed on this bar, for post-run analysis.
    macd_line: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    macd_signal: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    macd_hist: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))

    __table_args__ = (
        Index("idx_bar_aggregates_run_ts", "run_id", "ts", unique=True),
    )
