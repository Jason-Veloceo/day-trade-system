"""Stage-1 filter engine.

Given an active rule set (list[RuleSpec]) and a CandidateSnapshot, evaluates each
rule and returns the aggregate result. Hard-rule failures put the candidate into
`failed_filter`; soft failures still let it pass but are recorded.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from day_trade.filters.rules import RuleResult, RuleSpec, evaluate_rule
from day_trade.normalize.candidates import CandidateSnapshot


@dataclass(slots=True)
class FilterDecision:
    passed: bool
    failed_rules: list[str]
    results: list[RuleResult]


def evaluate(
    rules: list[RuleSpec],
    snap: CandidateSnapshot,
    *,
    news_datetime: dt.datetime | None = None,
    news_headline: str | None = None,
    now: dt.datetime | None = None,
) -> FilterDecision:
    results: list[RuleResult] = []
    failed_hard: list[str] = []
    for rule in rules:
        if not rule.enabled:
            continue
        r = evaluate_rule(
            rule, snap, news_datetime=news_datetime, news_headline=news_headline, now=now
        )
        results.append(r)
        if not r.passed and r.severity == "hard":
            failed_hard.append(r.rule_key)
    return FilterDecision(passed=not failed_hard, failed_rules=failed_hard, results=results)
