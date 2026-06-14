"""Observe DTD network responses for `/alert?widget=...` payloads.

We attach a response handler to the Playwright context. Whenever a JSON response
arrives for a configured widget endpoint, we parse it into RawDtdEvents, filter
out events we've already seen (by ts per widget), and feed the new ones into the
ingestion pipeline + pub/sub broker.

We do NOT call DTD endpoints ourselves - we only observe what the DTD page polls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Response

from day_trade.config import get_settings
from day_trade.ingest.dtd.parser import parse_alert_response
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
    # https://scan-prod.warriortrading.com/alert?widget=Momo&...
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
    """Attach to a Playwright BrowserContext and emit RawDtdEvents."""

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

    def attach(self) -> None:
        if self._attached:
            return
        self._context.on("response", self._handle)
        self._attached = True
        logger.info(
            "DTD observer attached. host=%s widgets=%s", self._api_host, sorted(self._widgets)
        )

    async def detach(self) -> None:
        if not self._attached:
            return
        self._context.remove_listener("response", self._handle)
        self._attached = False
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    def _handle(self, response: Response) -> None:
        url = response.url
        if self._api_host not in url:
            return
        widget = _widget_from_url(url)
        if widget is None or widget not in self._widgets:
            return
        task = asyncio.create_task(self._process(response, widget))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, response: Response, widget: str) -> None:
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
            logger.exception("Failed to parse DTD response for widget=%s", widget)
            return

        state = self._state.by_widget.setdefault(widget, WidgetState())
        new_events = [e for e in events if int(e.ts.timestamp() * 1000) > state.last_ts_ms]
        if not new_events:
            return

        new_events.sort(key=lambda e: e.ts)
        for ev in new_events:
            try:
                await self._on_event(ev)
            except Exception:
                logger.exception("on_event failed for symbol=%s ts=%s", ev.symbol, ev.ts)
                continue
            ts_ms = int(ev.ts.timestamp() * 1000)
            if ts_ms > state.last_ts_ms:
                state.last_ts_ms = ts_ms
            state.seen_count += 1

        logger.debug(
            "DTD widget=%s emitted %d new events (total seen=%d)",
            widget,
            len(new_events),
            state.seen_count,
        )


async def default_event_sink(event: RawDtdEvent) -> None:
    """Default sink wired in by the live-ingest entrypoint."""
    from day_trade.db.repositories.pipeline import ingest_event
    from day_trade.db.session import session_scope
    from day_trade.ws.broker import get_broker
    from day_trade.ws.topics import CANDIDATE_UPDATE, SCANNER_EVENT

    async for session in session_scope():
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
