"""Tests for the SharedWorker interception + observer socket-path ingestion.

The SharedWorkerInterceptor and DtdObserver socket ingress are unit-tested
here by driving them directly with sample JSON payloads captured from a
real Warrior Trading SharedWorker session
(see scripts/dtd_diagnose_ws.py). No real browser is required — we bypass
Playwright by constructing the interceptor without calling `install()` and
invoking `_dispatch()` directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from day_trade.ingest.dtd.observer import DtdObserver
from day_trade.ingest.dtd.parser import parse_single_alert
from day_trade.ingest.dtd.shared_worker import SharedWorkerInterceptor


# --- Sample payloads copied from live capture (see ws_diagnose.jsonl) ---

_SAMPLE_ALERT_BODY: dict[str, Any] = {
    "symbol": "US-TC",
    "widget": "Running_Up",
    "strategy": "Running_Up_Alerts",
    "event": "0x700",
    "fields": [
        {"val": "06:12:43 am", "id": "Time"},
        {"val": "https://www.warriortrading.com/quote/TC/", "id": "Symbol"},
        {"val": 6.43, "id": "Close Price"},
        {"val": 22832218, "id": "Volume Today"},
        {"val": 16692903, "id": "Float"},
        {"val": 1086453.28, "id": "Rel Vol - Today"},
        {"val": 5143376.34, "id": "Rel Vol - 5 Min"},
        {"val": 247.56, "id": "Rel Gap"},
        {"val": 247.56, "id": "Rel Gain/Loss"},
        {"val": 5597, "id": "Short Interest"},
        {"val": "Running Up Alerts", "id": "Strategy"},
    ],
    "day": 20260701,
    "ts": 1782900763000,
}


def _msg_scanner_alert(body: dict[str, Any]) -> dict[str, Any]:
    """Build a full SharedWorker->window.__DTD_WORKER_MSG payload."""
    return {
        "source": "SharedWorker",
        "url": "/worker-server.158.js",
        "data": {
            "clientId": "abc",
            "socketName": "scanner",
            "type": "onMessage",
            "payload": {
                "channel": {"provider": "alert"},
                "event": "created",
                "body": body,
            },
        },
    }


def _msg_heartbeat() -> dict[str, Any]:
    return {
        "source": "SharedWorker",
        "url": "/worker-server.158.js",
        "data": {
            "clientId": "abc",
            "socketName": "scanner",
            "type": "onMessage",
            "payload": {
                "channel": {"provider": "sio-sys-heartbeat"},
                "event": "created",
                "body": {"timestamp": 1782900722369},
            },
        },
    }


def _msg_chart_tick() -> dict[str, Any]:
    return {
        "source": "SharedWorker",
        "url": "/worker-server.158.js",
        "data": {
            "clientId": "abc",
            "socketName": "scanner",
            "type": "onMessage",
            "payload": {
                "channel": {"provider": "chart-60s"},
                "event": "created",
                "body": {"close": 9.07, "time": 1782900726393, "volume": 38852},
            },
        },
    }


def _msg_chatroom_message() -> dict[str, Any]:
    return {
        "source": "SharedWorker",
        "url": "/worker-server.158.js",
        "data": {
            "clientId": "abc",
            "socketName": "chatroom",
            "type": "onMessage",
            "payload": {
                "channel": {"provider": "rooms/100003"},
                "event": "rt-user-list usersLength",
                "body": {"length": 1355},
            },
        },
    }


def _msg_worker_recaptcha() -> dict[str, Any]:
    # Google reCAPTCHA fires 'recaptcha-setup' plain string messages via a
    # regular Worker — these must be dropped, not raise.
    return {
        "source": "Worker",
        "url": "https://www.google.com/recaptcha/api2/webworker.js",
        "data": "recaptcha-setup",
    }


# --- parse_single_alert ---


def test_parse_single_alert_matches_full_response_first_element() -> None:
    ev = parse_single_alert(_SAMPLE_ALERT_BODY)
    assert ev.symbol == "TC"
    assert ev.widget == "Running_Up"
    assert ev.strategy == "Running_Up_Alerts"
    assert ev.strategy_label == "Running Up Alerts"
    assert ev.event == "0x700"
    # ts_ms 1782900763000 -> UTC 2026-07-01T10:12:43+00:00
    assert int(ev.ts.timestamp() * 1000) == 1782900763000
    assert ev.trading_day.isoformat() == "2026-07-01"
    assert ev.close_price is not None
    assert ev.volume_today == 22832218
    assert ev.float_shares == 16692903
    assert ev.short_interest == 5597


# --- SharedWorkerInterceptor._dispatch (filter chain) ---


@pytest.mark.asyncio
async def test_dispatch_forwards_scanner_alert() -> None:
    ctx = MagicMock()
    seen: list[dict[str, Any]] = []

    async def on_alert(body: dict[str, Any]) -> None:
        seen.append(body)

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=on_alert)
    await interceptor._dispatch(_msg_scanner_alert(_SAMPLE_ALERT_BODY))

    assert len(seen) == 1
    assert seen[0]["symbol"] == "US-TC"
    assert interceptor.total_alerts == 1
    assert interceptor.total_messages == 1
    assert interceptor.last_alert_ts_ms == 1782900763000


@pytest.mark.asyncio
async def test_dispatch_drops_heartbeat_but_counts_it() -> None:
    ctx = MagicMock()
    seen: list[dict[str, Any]] = []

    async def on_alert(body: dict[str, Any]) -> None:
        seen.append(body)

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=on_alert)
    await interceptor._dispatch(_msg_heartbeat())

    assert seen == []
    assert interceptor.total_alerts == 0
    assert interceptor.total_heartbeats == 1


@pytest.mark.asyncio
async def test_dispatch_drops_chart_ticks_but_counts_them() -> None:
    ctx = MagicMock()
    seen: list[dict[str, Any]] = []

    async def on_alert(body: dict[str, Any]) -> None:
        seen.append(body)

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=on_alert)
    await interceptor._dispatch(_msg_chart_tick())

    assert seen == []
    assert interceptor.total_alerts == 0
    assert interceptor.total_chart_ticks == 1


@pytest.mark.asyncio
async def test_dispatch_drops_chatroom_socket_messages() -> None:
    ctx = MagicMock()
    seen: list[dict[str, Any]] = []

    async def on_alert(body: dict[str, Any]) -> None:
        seen.append(body)

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=on_alert)
    await interceptor._dispatch(_msg_chatroom_message())

    assert seen == []
    assert interceptor.total_alerts == 0


@pytest.mark.asyncio
async def test_dispatch_ignores_non_object_data() -> None:
    """Google reCAPTCHA fires plain-string message data; must not raise."""
    ctx = MagicMock()
    seen: list[dict[str, Any]] = []

    async def on_alert(body: dict[str, Any]) -> None:
        seen.append(body)

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=on_alert)
    await interceptor._dispatch(_msg_worker_recaptcha())

    assert seen == []
    assert interceptor.total_dropped == 1


@pytest.mark.asyncio
async def test_dispatch_callback_errors_dont_propagate() -> None:
    ctx = MagicMock()
    calls = {"n": 0}

    async def broken_on_alert(body: dict[str, Any]) -> None:
        calls["n"] += 1
        raise RuntimeError("simulated pipeline failure")

    interceptor = SharedWorkerInterceptor(ctx, on_alert_body=broken_on_alert)
    # Must not raise even though the callback does.
    await interceptor._dispatch(_msg_scanner_alert(_SAMPLE_ALERT_BODY))
    await interceptor._dispatch(_msg_scanner_alert(_SAMPLE_ALERT_BODY))

    assert calls["n"] == 2
    # Both counted as alerts; callback exception should not incremement `dropped`
    assert interceptor.total_alerts == 2
    assert interceptor.total_dropped == 0


# --- DtdObserver._ingest_alert_body (widget filter + dedup) ---


@pytest.mark.asyncio
async def test_observer_socket_path_widget_filter() -> None:
    ctx = MagicMock()
    seen = []

    async def on_event(ev) -> None:
        seen.append(ev)

    observer = DtdObserver(
        ctx, on_event=on_event, api_host="scan-prod.warriortrading.com",
        widgets=["Momo"],  # NOT the widget in the sample body
    )
    await observer._ingest_alert_body(_SAMPLE_ALERT_BODY)
    assert seen == []  # widget=Running_Up got filtered out


@pytest.mark.asyncio
async def test_observer_socket_path_emits_matching_widget() -> None:
    ctx = MagicMock()
    seen = []

    async def on_event(ev) -> None:
        seen.append(ev)

    observer = DtdObserver(
        ctx, on_event=on_event, api_host="scan-prod.warriortrading.com",
        widgets=["Running_Up", "Momo"],
    )
    await observer._ingest_alert_body(_SAMPLE_ALERT_BODY)
    assert len(seen) == 1
    assert seen[0].symbol == "TC"


@pytest.mark.asyncio
async def test_observer_socket_path_dedupes_by_ts() -> None:
    """Same body dispatched twice should only emit once (ts not > last_ts_ms)."""
    ctx = MagicMock()
    seen = []

    async def on_event(ev) -> None:
        seen.append(ev)

    observer = DtdObserver(
        ctx, on_event=on_event, api_host="scan-prod.warriortrading.com",
        widgets=["Running_Up"],
    )
    await observer._ingest_alert_body(_SAMPLE_ALERT_BODY)
    await observer._ingest_alert_body(_SAMPLE_ALERT_BODY)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_observer_socket_path_emits_later_ts() -> None:
    """A later-ts body should emit even after an earlier one landed."""
    ctx = MagicMock()
    seen = []

    async def on_event(ev) -> None:
        seen.append(ev)

    observer = DtdObserver(
        ctx, on_event=on_event, api_host="scan-prod.warriortrading.com",
        widgets=["Running_Up"],
    )
    await observer._ingest_alert_body(_SAMPLE_ALERT_BODY)
    later = dict(_SAMPLE_ALERT_BODY)
    later["ts"] = _SAMPLE_ALERT_BODY["ts"] + 1000  # +1s
    await observer._ingest_alert_body(later)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_observer_socket_path_ignores_malformed_body() -> None:
    ctx = MagicMock()
    seen = []

    async def on_event(ev) -> None:
        seen.append(ev)

    observer = DtdObserver(
        ctx, on_event=on_event, api_host="scan-prod.warriortrading.com",
        widgets=["Running_Up"],
    )
    # Missing widget field
    await observer._ingest_alert_body({"symbol": "US-TC", "ts": 123, "day": 20260701})
    assert seen == []
    # Missing required parser fields (widget/strategy) present in dict but invalid schema
    bad = dict(_SAMPLE_ALERT_BODY)
    bad["fields"] = "not-a-list"
    await observer._ingest_alert_body(bad)
    assert seen == []
