"""Tests for the journal payload normaliser.

Regression test for the bug where decision events containing
`GateFailure` dataclass instances (and `GateFailureCategory` Enum
members) couldn't be JSON-encoded, causing the journal to fall back to
the diagnostic envelope `{"_error": "payload_not_serialisable", ...}`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
from decimal import Decimal

from day_trade.engine.journal import _jsonable
from day_trade.engine.strategies.first_pullback_long import (
    GateFailure,
    GateFailureCategory,
)


def test_jsonable_handles_gate_failure_dataclass() -> None:
    failure = GateFailure(
        category=GateFailureCategory.MICROSTRUCTURE,
        message="spread 56.9bps > max 50.0bps",
    )
    out = _jsonable(failure)
    assert out == {
        "category": "microstructure",
        "message": "spread 56.9bps > max 50.0bps",
    }
    json.dumps(out)


def test_jsonable_handles_microstructure_decision_payload() -> None:
    """Shape that the real microstructure-gate decision payload takes."""
    payload = {
        "stage": "microstructure_gate",
        "passed": False,
        "failures": [
            GateFailure(GateFailureCategory.MICROSTRUCTURE, "spread 56.9bps > max 50.0bps"),
            GateFailure(GateFailureCategory.MICROSTRUCTURE, "bid:ask imbalance 0.50 < min 0.55"),
        ],
        "notes": {
            "spread_bps": 56.872,
            "bid_ask_imbalance": 0.5,
            "tape_buy_pct_60s": 0.615,
        },
    }
    out = _jsonable(payload)
    encoded = json.dumps(out)
    decoded = json.loads(encoded)
    assert decoded["stage"] == "microstructure_gate"
    assert decoded["passed"] is False
    assert decoded["failures"] == [
        {"category": "microstructure", "message": "spread 56.9bps > max 50.0bps"},
        {"category": "microstructure", "message": "bid:ask imbalance 0.50 < min 0.55"},
    ]
    assert decoded["notes"]["spread_bps"] == 56.872


def test_jsonable_handles_nested_dataclass_and_enum() -> None:
    class Color(enum.Enum):
        RED = "red"
        BLUE = "blue"

    @dataclasses.dataclass
    class Inner:
        colour: Color
        amount: Decimal

    @dataclasses.dataclass
    class Outer:
        when: dt.datetime
        items: list[Inner]

    payload = Outer(
        when=dt.datetime(2026, 6, 30, 13, 44, tzinfo=dt.timezone.utc),
        items=[
            Inner(colour=Color.RED, amount=Decimal("1.50")),
            Inner(colour=Color.BLUE, amount=Decimal("2.75")),
        ],
    )
    out = _jsonable(payload)
    json.dumps(out)
    assert out["when"].startswith("2026-06-30T13:44")
    assert out["items"] == [
        {"colour": "red", "amount": "1.50"},
        {"colour": "blue", "amount": "2.75"},
    ]


def test_jsonable_preserves_primitives_and_containers() -> None:
    payload = {
        "an_int": 42,
        "a_float": 3.14,
        "a_str": "hello",
        "a_bool": True,
        "none": None,
        "a_list": [1, 2, {"deep": GateFailureCategory.TRIGGER}],
        "a_tuple": (1, 2, 3),
        "a_set": {1, 2, 3},
    }
    out = _jsonable(payload)
    encoded = json.dumps(out)
    decoded = json.loads(encoded)
    assert decoded["an_int"] == 42
    assert decoded["a_str"] == "hello"
    assert decoded["a_list"][2]["deep"] == "trigger"
    assert sorted(decoded["a_set"]) == [1, 2, 3]
