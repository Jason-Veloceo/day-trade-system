"""Default rule set seeded on first boot.

Tuned to a Ross-style starting point. User edits from the Rules page after that;
defaults are only used if no `is_active=true` rule set exists.
"""

from __future__ import annotations

from day_trade.config import Settings
from day_trade.filters.rules import RuleSpec


def default_rules(settings: Settings) -> list[RuleSpec]:
    return [
        RuleSpec(
            rule_key="price_range",
            field="last_close_price",
            op="between",
            value={"min": settings.default_min_price, "max": settings.default_max_price},
        ),
        RuleSpec(
            rule_key="max_float",
            field="last_float",
            op="le",
            value=settings.default_max_float,
        ),
        RuleSpec(
            rule_key="min_rel_vol_today",
            field="last_rel_vol_today",
            op="ge",
            value=settings.default_min_rel_vol_today,
        ),
        RuleSpec(
            rule_key="min_rel_vol_5min",
            field="last_rel_vol_5min",
            op="ge",
            value=settings.default_min_rel_vol_5min,
        ),
        RuleSpec(
            rule_key="min_rel_gain",
            field="last_rel_gain",
            op="ge",
            value=settings.default_min_rel_gain,
        ),
        RuleSpec(
            rule_key="require_news_within",
            field="news_age_minutes",
            op="within_minutes",
            value=settings.default_require_news_within_minutes,
            severity="soft",  # Ross prefers but doesn't require fresh news for every setup
        ),
    ]
