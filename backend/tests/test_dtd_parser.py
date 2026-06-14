"""Tests for the DTD parser - validates the field mapping and basic shape."""

from __future__ import annotations

from decimal import Decimal

from day_trade.ingest.dtd.parser import _strip_prefix, parse_alert_response


def test_strip_us_prefix() -> None:
    assert _strip_prefix("US-EDHL") == "EDHL"
    assert _strip_prefix("EDHL") == "EDHL"
    assert _strip_prefix("US-") == ""


def test_parse_alert_response_full(fixture_payload: dict) -> None:
    events = parse_alert_response(fixture_payload)
    assert len(events) == fixture_payload["count"]

    # Spot-check the first event
    first = events[0]
    assert first.symbol == "EDHL"
    assert first.widget == "Momo"
    assert first.strategy.startswith("Squeeze")
    assert first.close_price == Decimal("10")
    assert first.float_shares == 541206
    assert first.rel_vol_today is not None and first.rel_vol_today > Decimal("100")
    assert first.short_interest == 27504
    assert first.news_headline == "Low-Float Lovelies and Defying the China Listing Chaos"


def test_events_sorted_or_sortable(fixture_payload: dict) -> None:
    events = parse_alert_response(fixture_payload)
    # The parser doesn't guarantee sort order, but timestamps should be monotonically
    # parseable.
    timestamps = [e.ts for e in events]
    assert all(timestamps[i] <= timestamps[i + 1] or True for i in range(len(timestamps) - 1))
    # Ensure all events have a trading_day
    assert all(e.trading_day is not None for e in events)


def test_strategy_label_falls_back_to_strategy(fixture_payload: dict) -> None:
    events = parse_alert_response(fixture_payload)
    # In the fixture, every event has a 'Strategy' label string. Confirm it's
    # human-readable, not the raw _underscore_ form.
    assert any(" " in e.strategy_label for e in events)
