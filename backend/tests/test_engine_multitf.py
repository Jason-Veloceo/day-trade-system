"""Tests for the 5-minute aggregator."""

from __future__ import annotations

import datetime as dt

import pytest

from day_trade.engine.multitf import HigherTimeframeAggregator, _bucket_close
from day_trade.engine.strategies.base import Bar


@pytest.fixture
def t0() -> dt.datetime:
    return dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def test_bucket_close_5m(t0: dt.datetime) -> None:
    # 14:31:00 close -> bucket close 14:35:00
    assert _bucket_close(t0.replace(minute=31), 5) == t0.replace(minute=35)
    # 14:35:00 close -> bucket close 14:40:00 (35 is in [35,40))
    assert _bucket_close(t0.replace(minute=35), 5) == t0.replace(minute=40)
    # 14:30:00 close -> bucket close 14:30:00 (30 is in [30,30))? no - 30 in [30,35) -> 14:35
    assert _bucket_close(t0.replace(minute=30), 5) == t0.replace(minute=35)


@pytest.mark.asyncio
async def test_emits_on_bucket_close(t0: dt.datetime) -> None:
    emitted: list[Bar] = []

    async def on_close(b: Bar) -> None:
        emitted.append(b)

    agg = HigherTimeframeAggregator(window_minutes=5, on_close=on_close)
    # Bucket [30,35) ends at 14:35. Bars with close-minute in {31,32,33,34}
    # belong to that bucket. Bar with close-minute 35 already belongs to
    # the NEXT bucket [35,40) and triggers the emit of the [30,35) bucket.
    base = t0.replace(minute=31)
    for i in range(4):  # minutes 31, 32, 33, 34
        await agg.push(
            Bar(
                ts=base + dt.timedelta(minutes=i),
                open=10.0 + i * 0.1,
                high=10.5 + i * 0.1,
                low=9.5 + i * 0.1,
                close=10.2 + i * 0.1,
                volume=100 * (i + 1),
            )
        )
    assert emitted == []  # still inside the bucket

    # Push the bar that starts the next bucket -> previous bucket emits.
    await agg.push(
        Bar(
            ts=t0.replace(minute=35),
            open=11.0, high=11.0, low=11.0, close=11.0, volume=50.0,
        )
    )
    assert len(emitted) == 1
    bar = emitted[0]
    assert bar.open == 10.0
    # Sum of volumes from the 4 contributing bars: 100+200+300+400 = 1000
    assert bar.volume == 1000.0
