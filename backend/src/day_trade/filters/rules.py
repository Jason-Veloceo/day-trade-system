"""Rule definitions + operator evaluation.

Rules are data (rows in `filter_rules`), not code. This module knows how to take
one rule + one candidate snapshot and produce a pass/fail result with the
observed value(s) so the UI can explain why something was killed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from day_trade.normalize.candidates import CandidateSnapshot


@dataclass(slots=True)
class RuleSpec:
    """Pure-Python representation of one rule. Mirrors the `filter_rules` row."""

    rule_key: str
    field: str
    op: str
    value: Any
    enabled: bool = True
    severity: str = "hard"  # 'hard' kills, 'soft' warns


@dataclass(slots=True)
class RuleResult:
    rule_key: str
    passed: bool
    observed: Any
    threshold: Any
    severity: str


# Fields that exist on the candidate snapshot. Extending this needs both a
# snapshot field and (ideally) a default rule.
KNOWN_FIELDS = {
    "last_close_price",
    "last_volume",
    "last_float",
    "last_rel_vol_today",
    "last_rel_vol_5min",
    "last_rel_gap",
    "last_rel_gain",
    "last_short_interest",
    "has_news",
    "latest_newsid",
    "news_age_minutes",  # derived from latest_news.datetime, see below
    "news_headline",  # derived from latest_news
    "strategies_fired",
    "widgets_fired",
    "is_5_pillars",
}


def _to_number(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_field(
    snap: CandidateSnapshot,
    field: str,
    *,
    news_datetime: dt.datetime | None,
    news_headline: str | None,
    now: dt.datetime,
) -> Any:
    if field == "news_age_minutes":
        if news_datetime is None:
            return None
        delta = now - news_datetime
        return max(0.0, delta.total_seconds() / 60.0)
    if field == "news_headline":
        return news_headline
    return getattr(snap, field, None)


def _eval_op(op: str, observed: Any, threshold: Any) -> bool:
    """Evaluate a single (op, observed, threshold) tuple.

    Returns True if the rule passes.

    Null observed (missing data) treated as fail for hard numeric rules - we
    don't want to trust a candidate where the data isn't there yet. Boolean rules
    treat null as false.
    """
    if op in {"lt", "le", "gt", "ge", "eq"}:
        obs_n = _to_number(observed)
        thr_n = _to_number(threshold)
        if obs_n is None or thr_n is None:
            return False
        match op:
            case "lt":
                return obs_n < thr_n
            case "le":
                return obs_n <= thr_n
            case "gt":
                return obs_n > thr_n
            case "ge":
                return obs_n >= thr_n
            case "eq":
                return obs_n == thr_n

    if op == "between":
        # threshold = {"min": x, "max": y} (either bound may be null = no bound)
        obs_n = _to_number(observed)
        if obs_n is None:
            return False
        lo = threshold.get("min")
        hi = threshold.get("max")
        if lo is not None and obs_n < float(lo):
            return False
        if hi is not None and obs_n > float(hi):
            return False
        return True

    if op == "in":
        # threshold is a list of allowed values
        if isinstance(observed, list):
            return any(v in threshold for v in observed)
        return observed in threshold

    if op == "contains_any":
        # threshold = list of substrings; observed is a string (headline) or list of strings
        if observed is None:
            return False
        text = (observed if isinstance(observed, str) else " ".join(map(str, observed))).lower()
        return any(str(t).lower() in text for t in threshold)

    if op == "contains_none":
        if observed is None:
            return True
        text = (observed if isinstance(observed, str) else " ".join(map(str, observed))).lower()
        return not any(str(t).lower() in text for t in threshold)

    if op == "within_minutes":
        # observed = minutes, threshold = max minutes
        obs_n = _to_number(observed)
        thr_n = _to_number(threshold)
        if thr_n is None:
            return True
        if obs_n is None:
            return False
        return obs_n <= thr_n

    raise ValueError(f"Unknown rule op: {op}")


def evaluate_rule(
    rule: RuleSpec,
    snap: CandidateSnapshot,
    *,
    news_datetime: dt.datetime | None = None,
    news_headline: str | None = None,
    now: dt.datetime | None = None,
) -> RuleResult:
    when = now or dt.datetime.now(tz=dt.UTC)
    observed = _read_field(
        snap, rule.field, news_datetime=news_datetime, news_headline=news_headline, now=when
    )
    passed = _eval_op(rule.op, observed, rule.value) if rule.enabled else True
    return RuleResult(
        rule_key=rule.rule_key,
        passed=passed,
        observed=observed,
        threshold=rule.value,
        severity=rule.severity,
    )
