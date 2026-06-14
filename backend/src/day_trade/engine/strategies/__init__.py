"""Strategy registry.

Add new strategies here so the engine API can look them up by name.
"""

from __future__ import annotations

from .base import Bar, Signal, SignalKind, Strategy
from .first_pullback_long import FirstPullbackLong
from .macd_crossover_long import MACDCrossoverLong

STRATEGIES: dict[str, type[Strategy]] = {
    "macd_crossover_long": MACDCrossoverLong,
    "first_pullback_long": FirstPullbackLong,
}


def get_strategy(name: str) -> type[Strategy]:
    if name not in STRATEGIES:
        raise KeyError(
            f"unknown strategy {name!r}. available: {sorted(STRATEGIES.keys())}"
        )
    return STRATEGIES[name]


__all__ = ["Bar", "Signal", "SignalKind", "Strategy", "STRATEGIES", "get_strategy"]
