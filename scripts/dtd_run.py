"""Long-running DTD live ingestion process.

Launches headless Chromium with the persistent profile, navigates to the DTD
dashboard URL, attaches the network-response observer, and stays alive.

Each `/alert?widget=...` response that arrives is parsed, filtered for events
newer than the last-seen ts per widget, and pushed through the pipeline +
broadcast on the WS broker.

Run with:
    cd backend && uv run python ../scripts/dtd_run.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from day_trade.config import get_settings  # noqa: E402
from day_trade.ingest.dtd.browser import context_session, open_dtd_page  # noqa: E402
from day_trade.ingest.dtd.observer import build_observer  # noqa: E402


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
    )
    settings = get_settings()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with context_session(settings.playwright_profile_path, headless=settings.dtd_headless) as ctx:
        observer = build_observer(ctx)
        # attach() is now async: it installs the SharedWorker interceptor
        # (Playwright add_init_script + expose_function) as well as the
        # HTTP response handler.
        await observer.attach()
        page = await open_dtd_page(ctx, settings.dtd_login_url)

        # The persistent Chromium profile may restore pre-existing tabs
        # (e.g. the chatroom popup). Init scripts only apply to future
        # navigations, so pages restored BEFORE `observer.attach()` still
        # have an unwrapped `window.SharedWorker`. Reload every open page
        # so the wrapper is in place before the WT bundle constructs its
        # scanner SharedWorker.
        for p in list(ctx.pages):
            try:
                await p.reload(wait_until="domcontentloaded")
            except Exception:
                logging.exception("Failed to reload page %s during observer bootstrap", p.url)

        logging.info("DTD page open at %s. Waiting for alert traffic.", page.url)
        await stop.wait()
        logging.info("Shutting down DTD observer.")
        await observer.detach()


if __name__ == "__main__":
    asyncio.run(main())
