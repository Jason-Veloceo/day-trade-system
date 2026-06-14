"""Tests for per-symbol candidate dedupe and cooldown semantics."""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.normalize.candidates import apply_event


@pytest.fixture
def cooldown() -> dt.timedelta:
    return dt.timedelta(minutes=10)


def test_first_event_creates_candidate(make_event, cooldown) -> None:
    e = make_event("ABCD", ts=dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC))
    up = apply_event(None, e, cooldown=cooldown)
    assert up.is_new is True
    assert up.snapshot.symbol == "ABCD"
    assert up.snapshot.alert_count == 1
    assert up.snapshot.cooldown_until == e.ts + cooldown
    assert up.snapshot.widgets_fired == ["Momo"]


def test_event_inside_cooldown_merges(make_event, cooldown) -> None:
    base = dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC)
    e1 = make_event("ABCD", ts=base, strategy="Low_Float_High_Rel_Vol")
    first = apply_event(None, e1, cooldown=cooldown).snapshot

    e2 = make_event(
        "ABCD",
        ts=base + dt.timedelta(minutes=3),
        strategy="Squeeze_Alert_Up_10_Percent_in_10min_v2",
        widget="Momo",
    )
    up = apply_event(first, e2, cooldown=cooldown)

    assert up.is_new is False
    assert up.snapshot.alert_count == 2
    assert up.snapshot.last_alert_at == e2.ts
    assert set(up.snapshot.strategies_fired) == {
        "Low_Float_High_Rel_Vol",
        "Squeeze_Alert_Up_10_Percent_in_10min_v2",
    }
    # cooldown is anchored to first_alert_at - not extended by later alerts
    assert up.snapshot.cooldown_until == first.cooldown_until


def test_event_after_cooldown_creates_new_candidate(make_event, cooldown) -> None:
    base = dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC)
    e1 = make_event("ABCD", ts=base)
    first = apply_event(None, e1, cooldown=cooldown).snapshot

    e2 = make_event("ABCD", ts=base + dt.timedelta(minutes=15))
    up = apply_event(first, e2, cooldown=cooldown)

    assert up.is_new is True
    assert up.snapshot.alert_count == 1
    assert up.snapshot.first_alert_at == e2.ts


def test_five_pillars_tag_propagates(make_event, cooldown) -> None:
    base = dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC)
    first = apply_event(None, make_event("ABCD", ts=base, widget="Momo"), cooldown=cooldown).snapshot
    assert first.is_5_pillars is False

    e2 = make_event("ABCD", ts=base + dt.timedelta(minutes=1), widget="some-uuid-5p")
    up = apply_event(first, e2, cooldown=cooldown, five_pillars_widget_id="some-uuid-5p")
    assert up.snapshot.is_5_pillars is True


def test_widget_5pillars_named(make_event, cooldown) -> None:
    base = dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC)
    e = make_event("ABCD", ts=base, widget="5Pillars")
    up = apply_event(None, e, cooldown=cooldown)
    assert up.snapshot.is_5_pillars is True
