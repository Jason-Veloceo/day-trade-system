"""Detached-subprocess controller for the DTD observer.

Lifecycle model:

  - start() spawns `python scripts/dtd_run.py` as a new session leader
    (POSIX setsid) with stdin redirected to /dev/null and stdout/stderr
    redirected to a rotating-ish log file. The PID is persisted to a
    pidfile so a backend restart can re-attach to the existing child.
  - stop() reads the pidfile, sends SIGTERM (graceful — dtd_run.py
    catches it and detaches the observer cleanly), waits up to N
    seconds, then SIGKILL if still alive.
  - status() reads the pidfile, verifies the process is alive (via
    `os.kill(pid, 0)`), and queries the DB for the most recent
    scanner_events row so the UI can show "last event N seconds ago"
    — the single most useful health signal because the observer can
    be alive but ingesting nothing (DOM changed, Playwright page
    closed, etc.).

The pidfile and logfile live under `<repo>/var/` (created on demand).
This directory is NOT git-tracked (the repo's existing .gitignore
handles `var/` or we can add it later — for now the files are
created at runtime and don't pollute the working tree if missing).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from day_trade.config import get_settings
from day_trade.db.models import ScannerEvent
from day_trade.db.session import session_scope

logger = logging.getLogger(__name__)


# ---------------- value object ----------------


@dataclass(frozen=True, slots=True)
class DtdObserverStatus:
    """Snapshot returned by `controller.status()` and the
    `/dtd/observer/status` endpoint. All times are ISO-8601 UTC.
    """

    running: bool
    pid: int | None
    started_at: str | None  # when WE started this PID (file mtime proxy)
    last_event_at: str | None  # most recent row in scanner_events
    last_event_age_seconds: float | None
    log_path: str | None
    pidfile_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "last_event_age_seconds": self.last_event_age_seconds,
            "log_path": self.log_path,
            "pidfile_path": self.pidfile_path,
        }


# ---------------- controller ----------------


class DtdObserverController:
    """Process-management facade for `scripts/dtd_run.py`.

    Single instance per backend process (singleton via
    `get_controller()`); all operations are short-lived and
    synchronous-friendly so they're safe to call from FastAPI
    handlers.

    Concurrency note: start() / stop() are not protected by a lock.
    The pidfile-existence check is racey across simultaneous callers
    but the failure mode is benign (two starts → second one sees an
    alive PID and returns "already running"). If we ever need
    stronger guarantees we can wrap with an asyncio.Lock at the API
    layer.
    """

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        var_dir: Path | None = None,
    ) -> None:
        # Default repo_root: walk up from this file to find the repo
        # (this module lives at backend/src/day_trade/dtd_control/
        # controller.py, so 5 parents up is the repo root).
        if repo_root is None:
            repo_root = Path(__file__).resolve().parents[4]
        self._repo_root = repo_root
        self._var_dir = var_dir or (repo_root / "var")
        self._pidfile = self._var_dir / "dtd_observer.pid"
        self._logfile = self._var_dir / "dtd_observer.log"
        self._script = repo_root / "scripts" / "dtd_run.py"

    # ---- public API ----

    async def start(self) -> DtdObserverStatus:
        """Start the observer if not already running. Idempotent: if a
        live PID is found in the pidfile, returns the existing status
        without spawning a duplicate."""
        existing = self._read_pid()
        if existing is not None and _is_alive(existing):
            logger.info(
                "DtdObserverController.start: already running pid=%d", existing
            )
            return await self.status()

        # Stale pidfile cleanup
        if existing is not None:
            logger.info(
                "DtdObserverController.start: stale pidfile (pid=%d not alive); replacing",
                existing,
            )
            self._clear_pidfile()

        if not self._script.exists():
            raise FileNotFoundError(
                f"dtd_run.py not found at {self._script}; "
                "controller is misconfigured"
            )

        self._var_dir.mkdir(parents=True, exist_ok=True)
        # Append to logfile so prior runs' history is preserved across
        # restarts. The operator can tail this file to debug.
        logf = open(self._logfile, "ab", buffering=0)
        try:
            cwd = self._repo_root / "backend"
            # Use `uv run python <script>` to inherit the project's
            # virtualenv exactly the way the user runs it manually.
            # That keeps Playwright + the backend code on the same
            # interpreter / sys.path.
            cmd = ["uv", "run", "python", str(self._script)]
            logger.info(
                "DtdObserverController.start: spawning %s (cwd=%s log=%s)",
                cmd,
                cwd,
                self._logfile,
            )
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                # New session so SIGINT to uvicorn doesn't cascade,
                # and so uvicorn --reload (which kills the FastAPI
                # process) doesn't take the observer down with it.
                start_new_session=True,
                close_fds=True,
            )
        finally:
            # Popen duplicated the fd; close ours.
            logf.close()

        self._write_pid(proc.pid)
        logger.info("DtdObserverController.start: observer pid=%d", proc.pid)

        # Give the child a moment to actually start (or fail). If it
        # exits immediately we want the next status() to reflect that
        # honestly instead of "running".
        await asyncio.sleep(0.3)
        return await self.status()

    async def stop(self, *, timeout_seconds: float = 5.0) -> DtdObserverStatus:
        """Stop the observer if running. Sends SIGTERM, waits up to
        `timeout_seconds`, then SIGKILL if needed. Always returns the
        post-stop status (`running=False` on success)."""
        pid = self._read_pid()
        if pid is None:
            logger.info("DtdObserverController.stop: no pidfile; nothing to do")
            return await self.status()

        if not _is_alive(pid):
            logger.info(
                "DtdObserverController.stop: pid=%d not alive; clearing pidfile",
                pid,
            )
            self._clear_pidfile()
            return await self.status()

        logger.info("DtdObserverController.stop: SIGTERM pid=%d", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._clear_pidfile()
            return await self.status()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not _is_alive(pid):
                break
            await asyncio.sleep(0.1)

        if _is_alive(pid):
            logger.warning(
                "DtdObserverController.stop: pid=%d did not exit on SIGTERM; SIGKILL",
                pid,
            )
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Drain any zombie (we're still the parent until uvicorn
        # reloads). Without this, after SIGKILL the child sits in
        # zombie state and `os.kill(pid, 0)` reports it as alive,
        # leading status() to lie. If we're not the parent anymore
        # (post-reload) waitpid raises ECHILD which we ignore.
        for _ in range(40):  # ~1s budget
            if not _is_alive(pid):
                break
            _reap_if_child(pid)
            await asyncio.sleep(0.025)

        self._clear_pidfile()
        return await self.status()

    async def status(self) -> DtdObserverStatus:
        """Snapshot of the observer's current process + ingestion
        health. Safe to call frequently (~1Hz polling from UI)."""
        pid = self._read_pid()
        running = pid is not None and _is_alive(pid)

        started_at: str | None = None
        if running and self._pidfile.exists():
            ts = dt.datetime.fromtimestamp(
                self._pidfile.stat().st_mtime, tz=dt.timezone.utc
            )
            started_at = ts.isoformat()

        # If the PID is dead but the file still exists, clear it as a
        # side-effect of status() so the UI never sees a phantom
        # "running" state stick around.
        if pid is not None and not running:
            self._clear_pidfile()

        last_event_at, age = await _last_scanner_event_age()

        return DtdObserverStatus(
            running=running,
            pid=pid if running else None,
            started_at=started_at,
            last_event_at=last_event_at,
            last_event_age_seconds=age,
            log_path=str(self._logfile) if self._logfile.exists() else None,
            pidfile_path=str(self._pidfile),
        )

    # ---- pidfile helpers ----

    def _read_pid(self) -> int | None:
        if not self._pidfile.exists():
            return None
        try:
            raw = self._pidfile.read_text().strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "DtdObserverController: pidfile %s contains garbage %r; clearing",
                self._pidfile,
                raw,
            )
            self._clear_pidfile()
            return None

    def _write_pid(self, pid: int) -> None:
        self._var_dir.mkdir(parents=True, exist_ok=True)
        self._pidfile.write_text(f"{pid}\n")

    def _clear_pidfile(self) -> None:
        try:
            self._pidfile.unlink()
        except FileNotFoundError:
            pass


# ---------------- helpers ----------------


def _reap_if_child(pid: int) -> None:
    """Best-effort non-blocking reap. Idempotent. Safe to call even
    if the process is already gone or never was our child."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # Not our child (e.g. backend was reloaded so init now owns
        # the orphan). Nothing to reap from our side.
        pass
    except OSError:
        # Generic fallback; logging only — staleness checks via
        # _is_alive will still detect death.
        logger.exception("DtdObserverController: waitpid failed for pid=%d", pid)


def _is_alive(pid: int) -> bool:
    """Returns True iff a process with this PID currently exists and
    we have permission to signal it. `os.kill(pid, 0)` is the POSIX
    idiom for this check — it sends no actual signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but isn't ours. Treat as alive — we won't
        # try to manage it, but at least don't lie to the UI.
        return True
    return True


async def _last_scanner_event_age() -> tuple[str | None, float | None]:
    """Query the DB for the most recent scanner_events.ts. Returns
    (iso_ts, age_seconds) or (None, None) if the table is empty / the
    query fails. Errors are logged but never raised — observer status
    is a UI signal, not a critical path."""
    try:
        async with session_scope() as s:
            row = await s.execute(select(func.max(ScannerEvent.ts)))
            ts = row.scalar_one_or_none()
    except Exception:
        logger.exception("DtdObserverController: failed to query last scanner_event")
        return (None, None)

    if ts is None:
        return (None, None)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    return (ts.isoformat(), age)


# ---------------- process-wide singleton ----------------


_CONTROLLER: DtdObserverController | None = None


def get_controller() -> DtdObserverController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = DtdObserverController()
    return _CONTROLLER


def reset_controller_for_testing() -> None:
    global _CONTROLLER
    _CONTROLLER = None
