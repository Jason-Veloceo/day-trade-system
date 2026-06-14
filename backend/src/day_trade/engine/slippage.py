"""Slippage and latency math.

We measure execution quality as a first-class output of every fill so the
user can see exactly what they're paying for the convenience of automation.

Sign convention:
  - For LONG entries we paid more than the signal price -> POSITIVE slippage.
  - For LONG exits we received less than the signal price -> POSITIVE slippage.
  - I.e. positive slippage is always bad for the trader.

`slippage_cents` is in instrument quote units * 100 (cents for USD). For forex
quoted in 1.156795 form, "cents" effectively becomes "pip * 100" - we don't
try to normalise the unit because the UI shows both cents and bps and bps is
unit-free.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlippageReport:
    signal_price: float
    fill_price: float
    side: str           # "BUY" | "SELL"
    slippage_cents: float
    slippage_bps: float
    latency_ms: int


def compute(
    *,
    side: str,
    signal_price: float,
    signal_ts: dt.datetime,
    fill_price: float,
    fill_ts: dt.datetime,
) -> SlippageReport:
    if signal_price <= 0:
        raise ValueError(f"signal_price must be positive, got {signal_price}")
    side_norm = side.upper()
    if side_norm not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")

    # raw delta in price units (e.g. USD)
    raw_delta = fill_price - signal_price
    # for a SELL, positive delta means we got more than we asked - that's good.
    if side_norm == "SELL":
        raw_delta = -raw_delta

    slippage_cents = raw_delta * 100.0
    slippage_bps = (raw_delta / signal_price) * 10_000.0
    latency_ms = int((fill_ts - signal_ts).total_seconds() * 1000)

    return SlippageReport(
        signal_price=signal_price,
        fill_price=fill_price,
        side=side_norm,
        slippage_cents=slippage_cents,
        slippage_bps=slippage_bps,
        latency_ms=latency_ms,
    )
