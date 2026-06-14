"""One-shot interactive login to Day Trade Dash.

Opens a headed Chromium pointed at the DTD chatroom. Log in manually with your
credentials and (optionally) "remember me" - the cookies persist in the Playwright
profile directory so subsequent headless runs are already authenticated.

Close the window when done. Re-run only if your session expires.

Usage:
    cd backend && uv run python ../scripts/dtd_login.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from day_trade.config import get_settings  # noqa: E402
from day_trade.ingest.dtd.browser import context_session, open_dtd_page  # noqa: E402


async def main() -> None:
    settings = get_settings()
    profile = settings.playwright_profile_path
    print(f"Opening WT in headed mode. Profile: {profile}")
    print(f"Start URL: {settings.dtd_login_url}")
    print()
    print("Steps to complete in the browser window:")
    print("  1. Log in to Warrior Trading (and tick 'remember me' if available)")
    print("  2. From the member dashboard, click 'Click here to Enter'")
    print("  3. On the access page, click 'Click here to Enter the Platform'")
    print("  4. Accept the disclaimer")
    print("  5. Confirm the DTD chatroom loads (charts + scanner popup, if you set one up)")
    print("  6. Close the window. Cookies and disclaimer-acceptance will persist.")
    print()
    async with context_session(profile, headless=False) as context:
        # Log every page (incl. popup windows like the DTD scanners pop-out).
        context.on("page", lambda p: print(f"[page opened] {p.url or '(about:blank)'}"))
        await open_dtd_page(context, settings.dtd_login_url)
        await context.wait_for_event("close", timeout=0)


if __name__ == "__main__":
    asyncio.run(main())
