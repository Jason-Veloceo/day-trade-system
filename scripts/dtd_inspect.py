"""Inspect what DTD does in the browser.

Launches headed Chromium with the persisted profile, opens the WT member
dashboard, then logs every page/popup that appears and every network response
from the DTD API host. State is also written to JSON/JSONL files under
`./playwright_profile/_inspect/` so we can read it back from outside the
running process.

This is the diagnostic counterpart to `dtd_login.py`. Use it to:
  - Confirm cookies/SSO persisted from the initial login
  - See exactly which URLs the chatroom + scanner popup load
  - Capture sample `/alert?widget=...` payloads (if any are polled)
  - Verify the popup window is visible to Playwright's context listener

Run with:
    cd backend && uv run python ../scripts/dtd_inspect.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from playwright.async_api import Page, Response  # noqa: E402

from day_trade.config import get_settings  # noqa: E402
from day_trade.ingest.dtd.browser import context_session, open_dtd_page  # noqa: E402

INSPECT_DIR = REPO_ROOT / "playwright_profile" / "_inspect"
INTERESTING_HOSTS = {
    "scan-prod.warriortrading.com",
    "chatroom.warriortrading.com",
    "www.warriortrading.com",
    "warriortrading.com",
}


def log(msg: str) -> None:
    print(msg, flush=True)
    with (INSPECT_DIR / "events.log").open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def write_snapshot(pages: list[Page]) -> None:
    snap = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pages": [{"index": i, "url": p.url, "title_pending": True} for i, p in enumerate(pages)],
    }
    (INSPECT_DIR / "pages.json").write_text(json.dumps(snap, indent=2))


def append_network(record: dict) -> None:
    with (INSPECT_DIR / "network.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


async def handle_response(response: Response) -> None:
    try:
        url = response.url
    except Exception:
        return
    host = urlparse(url).hostname or ""
    if not any(host.endswith(h) for h in INTERESTING_HOSTS):
        return
    status = response.status
    try:
        headers = await response.all_headers()
    except Exception:
        headers = {}
    ctype = headers.get("content-type", "")

    body_preview: str | None = None
    body_len: int | None = None
    if "/alert" in url or "json" in ctype:
        try:
            raw = await response.body()
            body_len = len(raw)
            body_preview = raw[:400].decode("utf-8", errors="replace")
            # Save full bodies for the URLs we care about as fixtures.
            if "/alert?widget=" in url or url.endswith("/v1/scanner/config"):
                fname = (
                    "alert_" + url.split("widget=")[-1].split("&")[0] + ".json"
                    if "/alert?widget=" in url
                    else "scanner_config.json"
                )
                (INSPECT_DIR / fname).write_bytes(raw)
                log(f"[fixture-saved] {fname} ({body_len} B)")
        except Exception:
            body_preview = "(body unreadable)"

    record = {
        "ts": time.strftime("%H:%M:%S"),
        "method": response.request.method,
        "status": status,
        "url": url,
        "content_type": ctype,
        "body_len": body_len,
        "body_preview": body_preview,
    }
    append_network(record)
    tag = "ALERT" if "/alert" in url else "http"
    log(f"[{tag}] {status} {response.request.method} {url}  ({ctype}, {body_len} B)")


async def main() -> None:
    INSPECT_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("events.log", "network.jsonl", "pages.json"):
        (INSPECT_DIR / fname).write_text("")

    settings = get_settings()
    profile = settings.playwright_profile_path
    log(f"Inspect mode. Profile: {profile}")
    log(f"Start URL: {settings.dtd_login_url}")
    log("Click through gates if needed. Snapshot files: " + str(INSPECT_DIR))

    async with context_session(profile, headless=False) as context:

        def on_page(page: Page) -> None:
            log(f"[page] opened idx={len(context.pages)-1} url={page.url or '(blank)'}")
            page.on(
                "framenavigated",
                lambda frame: (
                    log(f"[nav] {frame.url}") if frame == page.main_frame else None
                ),
            )
            write_snapshot(list(context.pages))

        context.on("page", on_page)
        context.on("response", lambda r: asyncio.create_task(handle_response(r)))

        await open_dtd_page(context, settings.dtd_login_url)
        write_snapshot(list(context.pages))

        # Periodic snapshot in case popups/navigations are missed by the event hook
        async def snapshotter() -> None:
            while True:
                await asyncio.sleep(5)
                write_snapshot(list(context.pages))

        snap_task = asyncio.create_task(snapshotter())
        try:
            await context.wait_for_event("close", timeout=0)
        finally:
            snap_task.cancel()

        # On close, dump cookies for visibility (host-scoped only, no full values)
        try:
            cookies = await context.cookies()
            summary = [
                {"domain": c.get("domain"), "name": c.get("name"), "path": c.get("path")}
                for c in cookies
            ]
            (INSPECT_DIR / "cookies-summary.json").write_text(json.dumps(summary, indent=2))
            log(f"Cookies captured: {len(summary)} entries written to cookies-summary.json")
        except Exception as e:
            log(f"Could not dump cookies: {e}")


if __name__ == "__main__":
    asyncio.run(main())
