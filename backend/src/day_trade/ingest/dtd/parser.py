"""Parse a DTD `/alert?widget=...` response into our internal ScannerEvent stream.

Mostly a typed adapter over the raw shapes plus normalisation of the symbol prefix
('US-EDHL' -> 'EDHL') and the field-list -> attribute mapping.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from day_trade.ingest.dtd.types import DtdAlert, DtdAlertResponse
from day_trade.normalize.scanner_events import RawDtdEvent, ScannerEvent

logger = logging.getLogger(__name__)

# DTD field id -> our internal attribute name
FIELD_MAP: dict[str, str] = {
    "Time": "time_str",
    "Symbol": "symbol_url",
    "Close Price": "close_price",
    "Volume Today": "volume_today",
    "Float": "float_shares",
    "Rel Vol - Today": "rel_vol_today",
    "Rel Vol - 5 Min": "rel_vol_5min",
    "Rel Gap": "rel_gap",
    "Rel Gain/Loss": "rel_gain_loss",
    "Short Interest": "short_interest",
    "Strategy": "strategy_label",
}


def _strip_prefix(s: str) -> str:
    """'US-EDHL' -> 'EDHL'. Idempotent if already stripped."""
    return s.split("-", 1)[1] if s.startswith("US-") else s


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _ts_to_dt(ts_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC)


def _day_to_date(day_int: int) -> dt.date:
    s = str(day_int)
    return dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _index_fields(alert: DtdAlert) -> dict[str, Any]:
    """Turn the `fields: [{id, val}, ...]` list into a flat dict keyed by our internal names."""
    indexed: dict[str, Any] = {}
    for f in alert.fields:
        internal = FIELD_MAP.get(f.id)
        if internal:
            indexed[internal] = f.val
    return indexed


def alert_to_event(alert: DtdAlert) -> RawDtdEvent:
    """Convert one raw DTD alert into our internal RawDtdEvent (one row per fire)."""
    indexed = _index_fields(alert)
    symbol = _strip_prefix(alert.symbol)

    return RawDtdEvent(
        source="dtd",
        widget=alert.widget,
        strategy=alert.strategy,
        strategy_label=str(indexed.get("strategy_label") or alert.strategy),
        event=alert.event,
        symbol=symbol,
        ts=_ts_to_dt(alert.ts),
        trading_day=_day_to_date(alert.day),
        close_price=_to_decimal(indexed.get("close_price")),
        volume_today=_to_int(indexed.get("volume_today")),
        float_shares=_to_int(indexed.get("float_shares")),
        rel_vol_today=_to_decimal(indexed.get("rel_vol_today")),
        rel_vol_5min=_to_decimal(indexed.get("rel_vol_5min")),
        rel_gap=_to_decimal(indexed.get("rel_gap")),
        rel_gain_loss=_to_decimal(indexed.get("rel_gain_loss")),
        short_interest=_to_int(indexed.get("short_interest")),
        news_id=str(alert.news.newsid) if alert.news else None,
        news_headline=alert.news.headline if alert.news else None,
        news_storyurl=alert.news.storyurl if alert.news else None,
        news_datetime=dt.datetime.fromisoformat(alert.news.datetime) if alert.news else None,
        raw=alert.model_dump(mode="json"),
    )


def parse_alert_response(payload: bytes | str | dict[str, Any]) -> list[RawDtdEvent]:
    """Parse a full DTD alert-endpoint response into RawDtdEvents in chronological order."""
    if isinstance(payload, (bytes, str)):
        payload_dict = json.loads(payload)
    else:
        payload_dict = payload

    response = DtdAlertResponse.model_validate(payload_dict)
    return [alert_to_event(a) for a in response.data]


def to_scanner_event(raw: RawDtdEvent) -> ScannerEvent:
    """Drop the news payload (it's persisted separately) and expose the persistence shape."""
    return ScannerEvent(
        source=raw.source,
        widget=raw.widget,
        strategy=raw.strategy,
        strategy_label=raw.strategy_label,
        event=raw.event,
        symbol=raw.symbol,
        ts=raw.ts,
        trading_day=raw.trading_day,
        close_price=raw.close_price,
        volume_today=raw.volume_today,
        float_shares=raw.float_shares,
        rel_vol_today=raw.rel_vol_today,
        rel_vol_5min=raw.rel_vol_5min,
        rel_gap=raw.rel_gap,
        rel_gain_loss=raw.rel_gain_loss,
        short_interest=raw.short_interest,
        raw=raw.raw,
    )
