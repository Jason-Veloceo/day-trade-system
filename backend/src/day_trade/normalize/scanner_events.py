"""Internal event dataclasses.

`RawDtdEvent` is the intermediate type emitted by the DTD parser - it still carries
the joined news payload and the original JSON. Everything else downstream consumes
`ScannerEvent`, which is the storage shape.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True)
class RawDtdEvent:
    source: str
    widget: str
    strategy: str
    strategy_label: str
    event: str
    symbol: str
    ts: dt.datetime
    trading_day: dt.date

    close_price: Decimal | None
    volume_today: int | None
    float_shares: int | None
    rel_vol_today: Decimal | None
    rel_vol_5min: Decimal | None
    rel_gap: Decimal | None
    rel_gain_loss: Decimal | None
    short_interest: int | None

    news_id: str | None
    news_headline: str | None
    news_storyurl: str | None
    news_datetime: dt.datetime | None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScannerEvent:
    """One persisted scanner event."""

    source: str
    widget: str
    strategy: str
    strategy_label: str
    event: str
    symbol: str
    ts: dt.datetime
    trading_day: dt.date

    close_price: Decimal | None
    volume_today: int | None
    float_shares: int | None
    rel_vol_today: Decimal | None
    rel_vol_5min: Decimal | None
    rel_gap: Decimal | None
    rel_gain_loss: Decimal | None
    short_interest: int | None

    raw: dict[str, Any] = field(default_factory=dict)
