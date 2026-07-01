"""Diagnostic: capture EVERYTHING the DTD page does over the network.

Purpose: prove whether live scanner alerts come via HTTP polling, WebSocket
frames, or some other channel. Our current observer (observer.py) only
listens for HTTP responses. If we see WebSocket frames carrying alert
payloads here but no fresh /alert HTTP responses, the production
observer is missing the live feed and we know exactly what to fix.

This is a READ-ONLY diagnostic — it doesn't write to the DB or broker.
It runs for `DURATION_S` seconds and writes a structured JSONL file to
`playwright_profile/_inspect/ws_diagnose.jsonl` plus a human-readable
summary to stdout.

Run with (after stopping any other dtd_run.py instance, since they share
the same persistent profile):

    cd backend && uv run python ../scripts/dtd_diagnose_ws.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from playwright.async_api import Page, Request, Response, WebSocket  # noqa: E402

from day_trade.config import get_settings  # noqa: E402
from day_trade.ingest.dtd.browser import context_session, open_dtd_page  # noqa: E402

DURATION_S = 120  # capture longer so live updates have a chance to surface
OUT_DIR = REPO_ROOT / "playwright_profile" / "_inspect"
OUT_FILE = OUT_DIR / "ws_diagnose.jsonl"
ALERT_BODIES_DIR = OUT_DIR / "alert_bodies"

# Hosts we care about. WT serves the dashboard from warriortrading.com,
# scanner alerts (HTTP and possibly WS) from scan-prod, and the chatroom
# from chatroom.warriortrading.com.
INTERESTING_HOSTS = (
    "scan-prod.warriortrading.com",
    "chatroom.warriortrading.com",
    "warriortrading.com",
)


def _is_interesting(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host.endswith(h) for h in INTERESTING_HOSTS)


def _write(record: dict) -> None:
    with OUT_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _truncate(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_BODIES_DIR.mkdir(parents=True, exist_ok=True)
    # Clean previous bodies so we only have current-run data.
    for f in ALERT_BODIES_DIR.glob("*.json"):
        f.unlink()
    OUT_FILE.write_text("")

    settings = get_settings()
    print(f"[diag] starting {DURATION_S}s capture", flush=True)
    print(f"[diag] profile: {settings.playwright_profile_path}", flush=True)
    print(f"[diag] output : {OUT_FILE}", flush=True)
    print(
        "[diag] make sure dtd_run.py is STOPPED before running this "
        "(persistent profile is single-writer)",
        flush=True,
    )

    counts: Counter[str] = Counter()

    async with context_session(
        settings.playwright_profile_path, headless=settings.dtd_headless
    ) as context:

        # --- HTTP request START capture (so we see in-flight long-polls) ---
        # `request` fires when the browser INITIATES a request. If a request
        # is long-polling / SSE / chunked-streaming, the `response` event
        # may not fire during the capture window — but `request` always does.
        request_start_ts: dict[str, float] = {}

        def on_request(req: Request) -> None:
            url = req.url
            if not _is_interesting(url):
                return
            counts["http_request"] += 1
            request_start_ts[url + "#" + str(id(req))] = time.time()
            _write({
                "ts": _now_iso(),
                "kind": "http_request",
                "method": req.method,
                "url": url,
                "resource_type": req.resource_type,
            })

        context.on("request", on_request)

        def on_requestfinished(req: Request) -> None:
            url = req.url
            if not _is_interesting(url):
                return
            counts["http_requestfinished"] += 1

        context.on("requestfinished", on_requestfinished)

        def on_requestfailed(req: Request) -> None:
            url = req.url
            if not _is_interesting(url):
                return
            counts["http_requestfailed"] += 1
            _write({
                "ts": _now_iso(),
                "kind": "http_requestfailed",
                "method": req.method,
                "url": url,
                "failure": str(req.failure),
            })

        context.on("requestfailed", on_requestfailed)

        # --- HTTP response capture ---
        async def on_response(resp: Response) -> None:
            url = resp.url
            if not _is_interesting(url):
                return
            counts["http_response"] += 1
            try:
                ctype = (await resp.all_headers()).get("content-type", "")
            except Exception:
                ctype = ""
            body_preview: str | None = None
            full_body_saved: str | None = None
            if "/alert" in url or "json" in ctype:
                try:
                    raw = await resp.body()
                    body_preview = _truncate(raw.decode("utf-8", errors="replace"))
                    counts["http_alert"] += 1 if "/alert" in url else 0
                    # Persist full /alert bodies so we can inspect the
                    # TAIL of the response — that's where the latest
                    # events live, and shows whether /alert is the
                    # backlog channel or includes live tail too.
                    if "/alert" in url:
                        widget = url.split("widget=")[-1].split("&")[0]
                        fname = (
                            f"alert_{widget}_{int(time.time()*1000)}.json"
                        )
                        (ALERT_BODIES_DIR / fname).write_bytes(raw)
                        full_body_saved = fname
                except Exception:
                    body_preview = "(unreadable)"
            _write({
                "ts": _now_iso(),
                "kind": "http",
                "method": resp.request.method,
                "status": resp.status,
                "url": url,
                "content_type": ctype,
                "body_preview": body_preview,
                "full_body_saved_as": full_body_saved,
            })
            tag = "ALERT" if "/alert" in url else "http "
            print(f"[diag {tag}] {resp.status} {resp.request.method} {url}", flush=True)

        context.on(
            "response",
            lambda r: asyncio.create_task(on_response(r)),
        )

        # --- WebSocket capture: per-page since context.on("websocket")
        # isn't a thing in Playwright. We hook each page and each future
        # popup individually.
        def _attach_ws(page: Page) -> None:
            def on_ws(ws: WebSocket) -> None:
                wsurl = ws.url
                if not _is_interesting(wsurl):
                    # Capture metadata anyway in case interesting hosts
                    # delegate WS to another origin.
                    pass
                counts["ws_open"] += 1
                _write({
                    "ts": _now_iso(),
                    "kind": "ws_open",
                    "url": wsurl,
                })
                print(f"[diag WS  ] open {wsurl}", flush=True)

                def on_recv(payload: str | bytes) -> None:
                    counts["ws_frame_recv"] += 1
                    body: str
                    if isinstance(payload, bytes):
                        body = "(binary " + str(len(payload)) + " B) " + _truncate(
                            payload[:200].decode("utf-8", errors="replace"), 200
                        )
                    else:
                        body = _truncate(payload)
                    _write({
                        "ts": _now_iso(),
                        "kind": "ws_frame_recv",
                        "url": wsurl,
                        "payload": body,
                    })
                    # First few frames get echoed inline so the user
                    # can see structure live; afterwards we just count.
                    if counts["ws_frame_recv"] <= 10:
                        print(
                            f"[diag WS  ] frame#{counts['ws_frame_recv']} on {wsurl}: {body[:120]}",
                            flush=True,
                        )

                def on_sent(payload: str | bytes) -> None:
                    counts["ws_frame_sent"] += 1
                    body: str
                    if isinstance(payload, bytes):
                        body = "(binary " + str(len(payload)) + " B)"
                    else:
                        body = _truncate(payload, 200)
                    _write({
                        "ts": _now_iso(),
                        "kind": "ws_frame_sent",
                        "url": wsurl,
                        "payload": body,
                    })

                def on_close() -> None:
                    counts["ws_close"] += 1
                    _write({"ts": _now_iso(), "kind": "ws_close", "url": wsurl})
                    print(f"[diag WS  ] close {wsurl}", flush=True)

                ws.on("framereceived", on_recv)
                ws.on("framesent", on_sent)
                ws.on("close", on_close)

            page.on("websocket", on_ws)

        for p in context.pages:
            _attach_ws(p)
        context.on("page", _attach_ws)

        # --- Worker interception via injected init script ---
        # The WT scanner's live feed runs inside a **SharedWorker**
        # (`worker-server.158.js`, a Socket.IO client), verified from the
        # chatroom's main.js bundle:
        #     this._worker = new SharedWorker(e, {name: t, type: void 0})
        #     this._worker.port.addEventListener("message", ...)
        # SharedWorker contexts are even more isolated than regular Workers
        # (they're shared across tabs), and Playwright exposes NO events
        # for them. Our only leverage is JS-level monkey-patching before
        # the WT bundle constructs one.
        #
        # Strategy: wrap BOTH window.Worker and window.SharedWorker. For
        # SharedWorker we hook the `.port` MessagePort's `.onmessage` and
        # `addEventListener('message', ...)` calls, since that's how the
        # worker delivers scanner alerts back to the main page. Every
        # intercepted message is emitted via console.log with the tag
        # `__DTD_WORKER_MSG__`, which our page.on("console") listener
        # captures and persists to the diagnostic log.
        #
        # Whatever the underlying transport (Socket.IO WS, long-poll, SSE),
        # the payload delivered to the main page IS the live scanner data
        # — that's the source of truth for what the WT UI renders.
        INIT_SCRIPT = r"""
        (function() {
            const TAG = '__DTD_WORKER_MSG__';

            function safeStringify(data) {
                let s;
                try { s = JSON.stringify(data); }
                catch (_) { s = String(data); }
                if (s && s.length > 4000) {
                    s = s.slice(0, 4000) + '...(+' + (s.length - 4000) + ')';
                }
                return s;
            }

            function logMessage(source, url, data) {
                try {
                    console.log(TAG, JSON.stringify({
                        source: source,        // 'Worker' or 'SharedWorker'
                        url: String(url),
                        data: safeStringify(data),
                    }));
                } catch (_) {}
            }

            function wrapMessagePort(port, source, url) {
                if (!port || port.__dtdPortWrapped) return port;
                port.__dtdPortWrapped = true;

                // Intercept addEventListener('message', ...)
                const origAdd = port.addEventListener.bind(port);
                port.addEventListener = function(type, listener, options) {
                    if (type === 'message') {
                        const wrapped = function(e) {
                            logMessage(source, url, e && e.data);
                            return listener.call(this, e);
                        };
                        return origAdd(type, wrapped, options);
                    }
                    return origAdd(type, listener, options);
                };

                // Intercept onmessage= setter
                let realOnMessage = null;
                Object.defineProperty(port, 'onmessage', {
                    get() { return realOnMessage; },
                    set(fn) {
                        realOnMessage = fn;
                        origAdd('message', function(e) {
                            logMessage(source, url, e && e.data);
                            if (fn) fn.call(port, e);
                        });
                        // MessagePort auto-starts when onmessage is assigned,
                        // so we don't call .start() ourselves.
                    },
                    configurable: true,
                });

                return port;
            }

            // ---- Wrap window.Worker (regular Web Workers) ----
            const OriginalWorker = window.Worker;
            if (OriginalWorker && !OriginalWorker.__dtdWrapped) {
                function wrapWorker(url, opts) {
                    const w = new OriginalWorker(url, opts);
                    // For a regular Worker, `w` IS the message target
                    wrapMessagePort(w, 'Worker', url);
                    return w;
                }
                const WW = function(url, opts) { return wrapWorker(url, opts); };
                WW.prototype = OriginalWorker.prototype;
                WW.__dtdWrapped = true;
                window.Worker = WW;
            }

            // ---- Wrap window.SharedWorker (THIS is the WT scanner feed) ----
            const OriginalShared = window.SharedWorker;
            if (OriginalShared && !OriginalShared.__dtdWrapped) {
                function wrapShared(url, opts) {
                    const sw = new OriginalShared(url, opts);
                    // For a SharedWorker, messages come via `.port`
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

        await context.add_init_script(INIT_SCRIPT)

        # Console listener catches the worker messages we logged above.
        def _attach_console(page: Page) -> None:
            def on_console(msg):
                try:
                    args = msg.args
                    if not args:
                        return
                    # msg.text is faster than reading args
                    text = msg.text
                except Exception:
                    return
                if "__DTD_WORKER_MSG__" not in text:
                    return
                counts["worker_msg"] += 1
                # Strip tag prefix; keep JSON payload
                payload = text.replace("__DTD_WORKER_MSG__", "", 1).strip()
                _write({
                    "ts": _now_iso(),
                    "kind": "worker_msg",
                    "page_url": page.url,
                    "payload_preview": _truncate(payload, 1200),
                })
                if counts["worker_msg"] <= 15:
                    print(
                        f"[diag WMSG] #{counts['worker_msg']} on {page.url[:60]}: {_truncate(payload, 200)}",
                        flush=True,
                    )

            page.on("console", on_console)

        for p in context.pages:
            _attach_console(p)
        context.on("page", _attach_console)

        # Open the dashboard. The user's existing profile should still
        # be logged in. If a popup opens for the scanner, _attach_ws
        # will catch it via the context "page" event.
        page = await open_dtd_page(context, settings.dtd_login_url)
        print(f"[diag] page open; capturing for {DURATION_S}s...", flush=True)

        # Reload every already-open page so the init-script wrapper for
        # window.Worker takes effect. Init scripts only apply to future
        # navigations; existing tabs need a reload to pick them up. This
        # is important because a persistent Chromium profile may restore
        # the chatroom tab from a prior session — without a reload, its
        # workers were spawned by an UNWRAPPED window.Worker.
        for p in list(context.pages):
            try:
                await p.reload(wait_until="domcontentloaded")
            except Exception as e:
                print(f"[diag] reload failed for {p.url}: {e}", flush=True)

        await asyncio.sleep(DURATION_S)

    print("[diag] -------- SUMMARY --------", flush=True)
    for k, v in counts.most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"[diag] full log: {OUT_FILE}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
