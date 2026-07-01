"""SharedWorker interception for DTD's live scanner feed.

Warrior Trading's scanner and chatroom UI both run through a SINGLE
`SharedWorker` script (`chatroom.warriortrading.com/worker-server.158.js`)
which multiplexes two Socket.IO connections (`scanner` and `chatroom`).
Every live scanner alert the WT UI renders arrives through that
SharedWorker's `MessagePort`, NOT through the `/alert?widget=X` HTTP
endpoint (which is only queried ONCE per page load as a backlog snapshot).

Because SharedWorker contexts are fully isolated (they're shared across
tabs) and Playwright exposes no events for them, the ONLY way to observe
their traffic is to monkey-patch `window.SharedWorker` from an init
script BEFORE the WT bundle constructs one. The wrapper intercepts
`.port.onmessage` and `.port.addEventListener('message', ...)` so every
message the worker delivers back to the main page is forwarded to Python
via a Playwright-exposed function (`window.__DTD_WORKER_MSG`).

Verified schema (see scripts/dtd_diagnose_ws.py capture):

    {
      "clientId":   "<hex>",
      "socketName": "scanner" | "chatroom",
      "type":       "onConnect" | "onJoinResponsed" | "onMessage",
      "payload":    {
        "channel": {"provider": "alert" | "chart-60s" | "news" | ...},
        "event":   "created",
        "body":    { ...alert object matching /alert HTTP endpoint... },
      },
    }

The `body` for a `scanner/alert/created` message is byte-for-byte
identical to a single element of the existing HTTP `/alert?widget=X`
response's `data[]` list, so the same parsing pipeline (parser.py)
handles both.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)

# JS installed into every page/frame BEFORE user scripts run. Wraps both
# `window.Worker` and `window.SharedWorker`. The WT chatroom uses
# SharedWorker (verified in main.9f0be045.js:
# `this._worker = new SharedWorker(e, {name: t, type: void 0})`).
# We wrap plain Worker too for defensive coverage — recaptcha uses one,
# and future WT changes could migrate off SharedWorker.
_INIT_SCRIPT = r"""
(function() {
    function wrapMessagePort(port, source, url) {
        if (!port || port.__dtdPortWrapped) return port;
        port.__dtdPortWrapped = true;

        function emit(data) {
            // Fire-and-forget: don't block the message-handling path
            // on our Python callback. If the bridge isn't installed
            // yet (very early boot), silently drop.
            try {
                if (window.__DTD_WORKER_MSG) {
                    window.__DTD_WORKER_MSG({
                        source: source,
                        url: String(url),
                        data: data,
                    });
                }
            } catch (_) {}
        }

        const origAdd = port.addEventListener.bind(port);
        port.addEventListener = function(type, listener, options) {
            if (type === 'message') {
                const wrapped = function(e) {
                    emit(e && e.data);
                    return listener.call(this, e);
                };
                return origAdd(type, wrapped, options);
            }
            return origAdd(type, listener, options);
        };

        let realOnMessage = null;
        Object.defineProperty(port, 'onmessage', {
            get() { return realOnMessage; },
            set(fn) {
                realOnMessage = fn;
                origAdd('message', function(e) {
                    emit(e && e.data);
                    if (fn) fn.call(port, e);
                });
                // MessagePort auto-starts when onmessage is assigned;
                // don't call .start() ourselves.
            },
            configurable: true,
        });

        return port;
    }

    // Wrap window.Worker (regular Web Workers)
    const OriginalWorker = window.Worker;
    if (OriginalWorker && !OriginalWorker.__dtdWrapped) {
        function wrapWorker(url, opts) {
            const w = new OriginalWorker(url, opts);
            // For a regular Worker, `w` itself IS the message target
            wrapMessagePort(w, 'Worker', url);
            return w;
        }
        const WW = function(url, opts) { return wrapWorker(url, opts); };
        WW.prototype = OriginalWorker.prototype;
        WW.__dtdWrapped = true;
        window.Worker = WW;
    }

    // Wrap window.SharedWorker — this is what carries the WT scanner feed
    const OriginalShared = window.SharedWorker;
    if (OriginalShared && !OriginalShared.__dtdWrapped) {
        function wrapShared(url, opts) {
            const sw = new OriginalShared(url, opts);
            try { wrapMessagePort(sw.port, 'SharedWorker', url); }
            catch (_) {}
            return sw;
        }
        const SW = function(url, opts) { return wrapShared(url, opts); };
        SW.prototype = OriginalShared.prototype;
        SW.__dtdWrapped = true;
        window.SharedWorker = SW;
    }
})();
"""


# Signature of the alert-body callback: receives a single alert dict
# matching the DtdAlert schema (see parser.py / types.py).
AlertBodyCallback = Callable[[dict[str, Any]], Awaitable[None]]


class SharedWorkerInterceptor:
    """Attach a JS init script + Python bridge to a Playwright context.

    Once `install()` is awaited:
      - Every future navigation (including popups) has `window.SharedWorker`
        and `window.Worker` wrapped.
      - Every message the wrapped ports emit is delivered to Python via
        `window.__DTD_WORKER_MSG(...)`, which we filter and forward to
        `on_alert_body` for the scanner/alert channel only.

    Notes on already-loaded pages:
      Init scripts only apply to future navigations. If the context has
      pre-existing pages that already spawned the SharedWorker, those
      workers are missed until the pages are reloaded. `install()`
      does NOT force a reload — callers who care should reload explicitly
      after installation.
    """

    def __init__(self, context: BrowserContext, on_alert_body: AlertBodyCallback) -> None:
        self._context = context
        self._on_alert_body = on_alert_body
        self._installed = False
        # Rolling counters for observability
        self.total_messages = 0
        self.total_alerts = 0
        self.total_chart_ticks = 0
        self.total_heartbeats = 0
        self.total_dropped = 0
        self.last_alert_ts_ms: int | None = None
        # Per-widget alert counts so we can prove which widgets the
        # SharedWorker is actually delivering, independent of our
        # downstream widget filter. Used by /dtd/observer/status to
        # distinguish "socket subscribed but filtered" from "socket
        # never subscribed to that widget".
        self.alerts_by_widget: dict[str, int] = {}
        self._widget_first_logged: set[str] = set()

    async def install(self) -> None:
        if self._installed:
            return
        # 1. Expose Python callback to JS — must be BEFORE add_init_script
        #    so that when the injected wrapper first runs, the callback
        #    already exists (technically the wrapper guards with an
        #    `if (window.__DTD_WORKER_MSG)` check, but ordering here is
        #    cheap insurance).
        await self._context.expose_function(
            "__DTD_WORKER_MSG", self._dispatch
        )
        # 2. Register the JS wrapper for all future page navigations
        await self._context.add_init_script(_INIT_SCRIPT)
        self._installed = True
        logger.info("SharedWorkerInterceptor installed on context")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Called from JS via window.__DTD_WORKER_MSG(msg).

        Playwright serializes JS objects to Python dicts across the bridge.
        `msg` shape:
            {"source": "SharedWorker" | "Worker", "url": "...", "data": <any>}
        where `data` is the actual MessageEvent.data emitted by the worker.
        """
        self.total_messages += 1
        try:
            data = msg.get("data")
            if not isinstance(data, dict):
                self.total_dropped += 1
                return

            # Filter: only the "scanner" socket carries alerts
            socket_name = data.get("socketName")
            msg_type = data.get("type")
            if socket_name != "scanner" or msg_type != "onMessage":
                # Track heartbeat rate for health/liveness monitoring
                if msg_type == "onMessage" and socket_name == "scanner":
                    self.total_heartbeats += 1
                return

            payload = data.get("payload") or {}
            channel = (payload.get("channel") or {}).get("provider")
            event = payload.get("event")
            body = payload.get("body")

            if channel == "sio-sys-heartbeat":
                self.total_heartbeats += 1
                return
            if channel == "chart-60s":
                self.total_chart_ticks += 1
                return
            if channel != "alert" or event != "created" or not isinstance(body, dict):
                # Non-alert messages (news, toplist, alert-status) intentionally
                # dropped for the observer path — they belong to other pipelines.
                return

            # Live alert!
            self.total_alerts += 1
            ts = body.get("ts")
            if isinstance(ts, (int, float)):
                self.last_alert_ts_ms = int(ts)

            widget_name = body.get("widget")
            if isinstance(widget_name, str):
                self.alerts_by_widget[widget_name] = (
                    self.alerts_by_widget.get(widget_name, 0) + 1
                )
                if widget_name not in self._widget_first_logged:
                    self._widget_first_logged.add(widget_name)
                    logger.info(
                        "SharedWorker: first alert delivered for widget=%r "
                        "(symbol=%s ts_ms=%s)",
                        widget_name,
                        body.get("symbol"),
                        ts,
                    )

            try:
                await self._on_alert_body(body)
            except Exception:
                logger.exception(
                    "SharedWorkerInterceptor: on_alert_body callback raised "
                    "(symbol=%s widget=%s)",
                    body.get("symbol"),
                    body.get("widget"),
                )

        except Exception:
            logger.exception("SharedWorkerInterceptor: _dispatch failed")
            self.total_dropped += 1
