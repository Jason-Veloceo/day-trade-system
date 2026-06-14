"""Playwright persistent-context manager for DTD.

We launch a Chromium with a persistent profile so the user logs in interactively
once and the session cookies survive across runs. In normal operation it runs
headless; for the initial login we toggle headed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright


async def launch_persistent(
    profile_dir: Path,
    *,
    headless: bool,
    extra_args: list[str] | None = None,
) -> tuple[Any, BrowserContext]:
    """Open a persistent Chromium and return (playwright, context).

    Caller is responsible for closing context and stopping playwright. Use the
    `context_session` async-context helper below for the common case.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=extra_args or [],
    )
    return pw, context


@contextlib.asynccontextmanager
async def context_session(
    profile_dir: Path,
    *,
    headless: bool,
) -> AsyncIterator[BrowserContext]:
    """async with context_session(...) as context: ..."""
    pw, context = await launch_persistent(profile_dir, headless=headless)
    try:
        yield context
    finally:
        await context.close()
        await pw.stop()


async def open_dtd_page(context: BrowserContext, url: str) -> Page:
    """Open (or reuse) a tab pointed at the DTD dashboard URL."""
    pages = context.pages
    page = pages[0] if pages else await context.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    return page
