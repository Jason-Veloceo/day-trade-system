"""Observe DTD scanner alerts from BOTH the live SharedWorker feed and the
`/alert?widget=X` HTTP endpoint, dedupe, and emit RawDtdEvents to a callback.

Two ingress paths feed the same dedup+emit pipeline:

1. **SharedWorker socket (primary)** — Warrior Trading's UI runs a
   `SharedWorker` that multiplexes two Socket.IO streams. Every live
   scanner alert the WT UI renders arrives on the `scanner/alert/created`
   channel. `shared_worker.py` installs a JS interceptor that forwards
   these messages to the Python `_ingest_alert_body()` callback below.
   This is the LOW-LATENCY channel — messages typically arrive within
   ~100ms of WT's server firing the alert.

2. **HTTP `/alert?widget=X` response (secondary/backfill)** — the same
   response handler we've always had. WT only polls this endpoint once
   per page load, so it's effectively a startup backlog snapshot. We
   still ingest it because on cold-start we need history and because
   any /alert response the browser DOES fetch is free-to-consume data.

Both paths dedupe per widget by `ts_ms > state.last_ts_ms`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Response

from day_trade.config import get_settings
from day_trade.ingest.dtd.parser import parse_alert_response, parse_single_alert
from day_trade.ingest.dtd.shared_worker import SharedWorkerInterceptor
from day_trade.normalize.scanner_events import RawDtdEvent

logger = logging.getLogger(__name__)


@dataclass
class WidgetState:
    last_ts_ms: int = 0
    seen_count: int = 0


@dataclass
class ObserverState:
    by_widget: dict[str, WidgetState] = field(default_factory=dict)


EventCallback = Callable[[RawDtdEvent], Awaitable[None]]


def _widget_from_url(url: str) -> str | None:
    if "/alert" not in url:
        return None
    try:
        _, query = url.split("?", 1)
    except ValueError:
        return None
    for pair in query.split("&"):
        if pair.startswith("widget="):
            return pair[len("widget="):]
    return None


class DtdObserver:
    """Attach to a Playwright BrowserContext and emit RawDtdEvents.

    Ingests from two channels:
      - Live SharedWorker Socket.IO feed (primary, sub-second latency)
      - HTTP /alert?widget=X responses the browser makes on its own
        (secondary, only fires on page load / re-navigation)

    Widget filtering: only alerts whose `widget` matches one of the
    configured `widgets` list are emitted downstream. This mirrors the
    prior URL-based filter for the HTTP path — for the SharedWorker
    path we filter on the `body.widget` field, which is populated by
    WT's server for every alert.
    """

    def __init__(
        self,
        context: BrowserContext,
        on_event: EventCallback,
        *,
        api_host: str,
        widgets: list[str],
    ) -> None:
        self._context = context
        self._on_event = on_event
        self._api_host = api_host
        self._widgets = set(widgets)
        self._state = ObserverState()
        self._tasks: set[asyncio.Task] = set()
        self._attached = False
        self._socket_interceptor: SharedWorkerInterceptor | None = None

    async def attach(self) -> None:
        if self._attached:
            return
        # Install SharedWorker interceptor FIRST so its init script is
        # registered before any page navigates.
        self._socket_interceptor = SharedWorkerInterceptor(
            self._context,
            on_alert_body=self._ingest_alert_body,
        )
        await self._socket_interceptor.install()

        # Attach HTTP response handler (backlog / snapshot channel).
        self._context.on("response", self._handle_response)

        self._attached = True
        logger.info(
            "DTD observer attached (SharedWorker + HTTP). host=%s widgets=%s",
            self._api_host, sorted(self._widgets),
        )

    async def detach(self) -> None:
        if not self._attached:
            return
        self._context.remove_listener("response", self._handle_response)
        self._attached = False
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        # NOTE: init scripts registered on a context can't be un-registered.
        # If the same context is reused for another observer instance, the
        # SharedWorker wrapper will still be in place; SharedWorkerInterceptor
        # is idempotent so re-install is a no-op.

    def stats(self) -> dict[str, Any]:
        """Snapshot of ingestion counters (used for /dtd/observer/status)."""
        s = {
            "attached": self._attached,
            "widgets_configured": sorted(self._widgets),
            "widgets_seen": {
                w: {"last_ts_ms": st.last_ts_ms, "seen_count": st.seen_count}
                for w, st in self._state.by_widget.items()
            },
        }
        if self._socket_interceptor is not None:
            s["socket"] = {
                "total_messages": self._socket_interceptor.total_messages,
                "total_alerts": self._socket_interceptor.total_alerts,
                "total_chart_ticks": self._socket_interceptor.total_chart_ticks,
                "total_heartbeats": self._socket_interceptor.total_heartbeats,
                "total_dropped": self._socket_interceptor.total_dropped,
                "last_alert_ts_ms": self._socket_interceptor.last_alert_ts_ms,
                "alerts_by_widget": dict(self._socket_interceptor.alerts_by_widget),
            }
        return s

    # ------------------------------------------------------------------
    # HTTP path (existing behaviour, unchanged except moved into helpers)
    # ------------------------------------------------------------------

    def _handle_response(self, response: Response) -> None:
        url = response.url
        if self._api_host not in url:
            return
        widget = _widget_from_url(url)
        if widget is None or widget not in self._widgets:
            return
        task = asyncio.create_task(self._process_http(response, widget))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_http(self, response: Response, widget: str) -> None:
        try:
            if response.status >= 400:
                logger.warning("DTD response %s status=%s", response.url, response.status)
                return
            body = await response.body()
        except Exception:
            logger.exception("Failed to read DTD response body")
            return

        try:
            events = parse_alert_response(body)
        except Exception:
            logger.exception("Failed to parse DTD HTTP response for widget=%s", widget)
            return

        await self._emit_new_events(widget, events, source="http")

    # ------------------------------------------------------------------
    # SharedWorker path (new)
    # ------------------------------------------------------------------

    async def _ingest_alert_body(self, body: dict[str, Any]) -> None:
        """Callback invoked by SharedWorkerInterceptor for each scanner/alert/created message.

        `body` is a single DtdAlert dict. Widget filtering is done here
        because the socket stream is multiplexed — all widgets share
        one SharedWorker.
        """
        widget = body.get("widget")
        if not isinstance(widget, str) or widget not in self._widgets:
            return

        try:
            event = parse_single_alert(body)
        except Exception:
            logger.exception(
                "Failed to parse live SharedWorker alert body (widget=%s symbol=%s)",
                widget, body.get("symbol"),
            )
            return

        await self._emit_new_events(widget, [event], source="socket")

    # ------------------------------------------------------------------
    # Shared dedup + emit
    # ------------------------------------------------------------------

    async def _emit_new_events(
        self, widget: str, events: list[RawDtdEvent], *, source: str
    ) -> None:
        state = self._state.by_widget.setdefault(widget, WidgetState())
        new_events = [
            e for e in events if int(e.ts.timestamp() * 1000) > state.last_ts_ms
        ]
        if not new_events:
            return

        new_events.sort(key=lambda e: e.ts)
        for ev in new_events:
            try:
                await self._on_event(ev)
            except Exception:
                logger.exception(
                    "on_event failed src=%s symbol=%s ts=%s",
                    source, ev.symbol, ev.ts,
                )
                continue
            ts_ms = int(ev.ts.timestamp() * 1000)
            if ts_ms > state.last_ts_ms:
                state.last_ts_ms = ts_ms
            state.seen_count += 1

        logger.info(
            "DTD widget=%s src=%s emitted %d new events (total=%d)",
            widget, source, len(new_events), state.seen_count,
        )


async def default_event_sink(event: RawDtdEvent) -> None:
    """Default sink wired in by the live-ingest entrypoint."""
    from day_trade.db.repositories.pipeline import ingest_event
    from day_trade.db.session import session_scope
    from day_trade.ws.broker import get_broker
    from day_trade.ws.topics import CANDIDATE_UPDATE, SCANNER_EVENT

    async with session_scope() as session:
        result = await ingest_event(session, event)
        broker = get_broker()
        await broker.publish(
            SCANNER_EVENT,
            {
                "symbol": event.symbol,
                "widget": event.widget,
                "strategy": event.strategy,
                "ts": event.ts.isoformat(),
            },
        )
        await broker.publish(
            CANDIDATE_UPDATE,
            {
                "candidate_id": result.candidate_id,
                "symbol": result.snapshot.symbol,
                "status": "passed" if result.decision.passed else "failed_filter",
                "is_new": result.is_new_candidate,
                "failed_rules": result.decision.failed_rules,
                "last_alert_at": result.snapshot.last_alert_at.isoformat(),
            },
        )


def build_observer(context: BrowserContext) -> DtdObserver:
    settings = get_settings()
    return DtdObserver(
        context,
        on_event=default_event_sink,
        api_host=settings.dtd_api_host,
        widgets=settings.widget_list,
    )
