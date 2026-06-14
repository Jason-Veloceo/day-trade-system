"""Instrument contract builder for IBKR.

Auto-detects stock vs forex based on ticker shape. We do this for the POC so
the user can paste any ticker into the UI without thinking about whether to
pick "Stock" or "Forex"; the engine figures it out.

Heuristic:
  - Tickers containing "." (e.g. "EUR.USD") -> Forex on IDEALPRO
  - Anything else -> Stock on SMART, USD

This covers the only two instrument types the POC exercises. Futures,
options, and crypto are out of scope for the POC and would each get their own
branch here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ib_async import Contract, Forex, Stock

InstrumentType = Literal["stock", "forex"]


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """A normalised description of what we're trading.

    `display`     - what the user typed and what the UI shows ("SPY", "EUR.USD")
    `symbol`      - IBKR's wire symbol ("SPY", "EURUSD")
    `instrument`  - "stock" | "forex"
    `what_to_show`- IBKR bar source ("TRADES" for stocks, "MIDPOINT" for forex)
    `min_order_qty` - minimum order quantity for this instrument
    """

    display: str
    symbol: str
    instrument: InstrumentType
    what_to_show: str
    min_order_qty: int


def parse_instrument(ticker: str) -> InstrumentSpec:
    """Parse a user-entered ticker into an InstrumentSpec.

    >>> parse_instrument("SPY").instrument
    'stock'
    >>> parse_instrument("EUR.USD").instrument
    'forex'
    >>> parse_instrument("EUR.USD").symbol
    'EURUSD'
    """
    t = ticker.strip().upper()
    if not t:
        raise ValueError("ticker is empty")

    if "." in t:
        parts = t.split(".")
        if len(parts) != 2 or any(len(p) != 3 for p in parts):
            raise ValueError(
                f"forex ticker must be 'XXX.YYY' (3-letter ISO codes), got {ticker!r}"
            )
        return InstrumentSpec(
            display=t,
            symbol=parts[0] + parts[1],
            instrument="forex",
            what_to_show="MIDPOINT",
            min_order_qty=25_000,  # IBKR EUR.USD/major minimum
        )

    return InstrumentSpec(
        display=t,
        symbol=t,
        instrument="stock",
        what_to_show="TRADES",
        min_order_qty=1,
    )


def build_contract(spec: InstrumentSpec) -> Contract:
    """Build the IBKR contract for an InstrumentSpec.

    The caller is expected to qualify the contract via
    `ib.qualifyContractsAsync(...)` before using it for market data or orders.
    """
    if spec.instrument == "forex":
        return Forex(spec.symbol)
    return Stock(spec.symbol, "SMART", "USD")
