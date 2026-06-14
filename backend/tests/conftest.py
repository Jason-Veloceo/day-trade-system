"""Shared test fixtures."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from day_trade.normalize.scanner_events import RawDtdEvent

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "day_trade"
    / "ingest"
    / "dtd"
    / "fixtures"
    / "momo_response.json"
)


def _make_event(
    symbol: str,
    *,
    widget: str = "Momo",
    strategy: str = "Low_Float_Former_Momo_Stock",
    strategy_label: str = "Former Momo Stock",
    ts: dt.datetime | None = None,
    close: float | None = 5.0,
    float_shares: int | None = 5_000_000,
    rel_vol_today: float | None = 10.0,
    rel_vol_5min: float | None = 30.0,
    rel_gain: float | None = 50.0,
    has_news: bool = True,
    news_age_minutes: float = 30.0,
) -> RawDtdEvent:
    if ts is None:
        ts = dt.datetime(2026, 6, 12, 13, 30, tzinfo=dt.UTC)
    news_dt = ts - dt.timedelta(minutes=news_age_minutes) if has_news else None
    return RawDtdEvent(
        source="dtd",
        widget=widget,
        strategy=strategy,
        strategy_label=strategy_label,
        event="New High",
        symbol=symbol,
        ts=ts,
        trading_day=ts.date(),
        close_price=Decimal(str(close)) if close is not None else None,
        volume_today=100_000,
        float_shares=float_shares,
        rel_vol_today=Decimal(str(rel_vol_today)) if rel_vol_today is not None else None,
        rel_vol_5min=Decimal(str(rel_vol_5min)) if rel_vol_5min is not None else None,
        rel_gap=Decimal("5"),
        rel_gain_loss=Decimal(str(rel_gain)) if rel_gain is not None else None,
        short_interest=1000,
        news_id="abc123" if has_news else None,
        news_headline="Test news" if has_news else None,
        news_storyurl="http://example.com" if has_news else None,
        news_datetime=news_dt,
        raw={},
    )


@pytest.fixture
def make_event():
    return _make_event


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    return FIXTURE_PATH


@pytest.fixture(scope="session")
def fixture_payload(fixture_path: Path) -> dict:
    return json.loads(fixture_path.read_bytes())
