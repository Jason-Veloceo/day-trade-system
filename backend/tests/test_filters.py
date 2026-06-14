"""Tests for the Stage-1 filter engine."""

from __future__ import annotations

import datetime as dt

from day_trade.filters.engine import evaluate
from day_trade.filters.rules import RuleSpec, evaluate_rule
from day_trade.normalize.candidates import apply_event


def _snap(make_event, **kwargs):
    return apply_event(
        None, make_event("ABCD", **kwargs), cooldown=dt.timedelta(minutes=10)
    ).snapshot


def test_between_rule_in_range(make_event) -> None:
    snap = _snap(make_event, close=5.0)
    rule = RuleSpec("price_range", "last_close_price", "between", {"min": 1.5, "max": 20.0})
    r = evaluate_rule(rule, snap)
    assert r.passed is True


def test_between_rule_out_of_range(make_event) -> None:
    snap = _snap(make_event, close=25.0)
    rule = RuleSpec("price_range", "last_close_price", "between", {"min": 1.5, "max": 20.0})
    r = evaluate_rule(rule, snap)
    assert r.passed is False


def test_le_rule_against_float(make_event) -> None:
    snap = _snap(make_event, float_shares=50_000_000)
    rule = RuleSpec("max_float", "last_float", "le", 20_000_000)
    r = evaluate_rule(rule, snap)
    assert r.passed is False


def test_ge_rule_rel_vol(make_event) -> None:
    snap = _snap(make_event, rel_vol_today=10.0)
    rule = RuleSpec("min_rel_vol_today", "last_rel_vol_today", "ge", 3.0)
    r = evaluate_rule(rule, snap)
    assert r.passed is True


def test_within_minutes_with_news(make_event) -> None:
    base = dt.datetime(2026, 6, 12, 13, 0, tzinfo=dt.UTC)
    snap = _snap(make_event, ts=base, has_news=True, news_age_minutes=30)
    rule = RuleSpec("require_news_within", "news_age_minutes", "within_minutes", 240)
    r = evaluate_rule(
        rule, snap, news_datetime=base - dt.timedelta(minutes=30), now=base
    )
    assert r.passed is True


def test_within_minutes_no_news_fails(make_event) -> None:
    snap = _snap(make_event, has_news=False)
    rule = RuleSpec("require_news_within", "news_age_minutes", "within_minutes", 240)
    r = evaluate_rule(rule, snap, news_datetime=None)
    assert r.passed is False


def test_engine_aggregates_failures(make_event) -> None:
    snap = _snap(make_event, close=0.5, float_shares=100_000_000, rel_vol_today=0.5, rel_gain=1.0)
    rules = [
        RuleSpec("price_range", "last_close_price", "between", {"min": 1.5, "max": 20.0}),
        RuleSpec("max_float", "last_float", "le", 20_000_000),
        RuleSpec("min_rel_vol_today", "last_rel_vol_today", "ge", 3.0),
        RuleSpec("min_rel_gain", "last_rel_gain", "ge", 5.0),
    ]
    decision = evaluate(rules, snap)
    assert decision.passed is False
    assert set(decision.failed_rules) == {
        "price_range",
        "max_float",
        "min_rel_vol_today",
        "min_rel_gain",
    }


def test_engine_soft_rule_does_not_kill(make_event) -> None:
    snap = _snap(make_event, has_news=False)
    rules = [
        RuleSpec(
            "require_news_within",
            "news_age_minutes",
            "within_minutes",
            240,
            severity="soft",
        )
    ]
    decision = evaluate(rules, snap, news_datetime=None)
    assert decision.passed is True
    assert decision.failed_rules == []


def test_contains_any_keyword(make_event) -> None:
    snap = _snap(make_event)
    rule = RuleSpec(
        "news_substantive",
        "news_headline",
        "contains_any",
        ["FDA", "earnings", "data", "offering"],
    )
    r = evaluate_rule(rule, snap, news_headline="Company announces Phase 3 FDA data readout")
    assert r.passed is True

    r2 = evaluate_rule(rule, snap, news_headline="Routine corporate housekeeping notice")
    assert r2.passed is False
