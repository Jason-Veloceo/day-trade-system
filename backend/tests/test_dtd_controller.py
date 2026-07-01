"""Tests for the DTD observer subprocess controller.

We test the process-management mechanics with a tiny stand-in script
that just sleeps, so we exercise the real Popen + SIGTERM path without
needing Playwright or Chromium.

The DB-querying portion of status() is exercised separately with a
mocked `_last_scanner_event_age`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from day_trade.dtd_control import controller as ctrl_mod
from day_trade.dtd_control.controller import (
    DtdObserverController,
    _is_alive,
)


# ---------------- helpers ----------------


@pytest.fixture
def tmp_controller(tmp_path: Path) -> DtdObserverController:
    """A controller wired to a fake repo layout under tmp_path with a
    sleeping script standing in for dtd_run.py."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fake_script = scripts / "dtd_run.py"
    fake_script.write_text(
        "import signal, sys, time\n"
        "def _term(*a):\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _term)\n"
        "print('fake observer started', flush=True)\n"
        "time.sleep(120)\n"
    )
    (tmp_path / "backend").mkdir()
    c = DtdObserverController(repo_root=tmp_path, var_dir=tmp_path / "var")
    # The real start() uses `uv run python <script>` so the venv is
    # honoured. For tests we want to run with the current interpreter
    # so we don't depend on uv being installed under tmp_path. Patch
    # the Popen call site by overriding _script and the command.
    # Simplest path: monkeypatch the command builder inline.
    return c


# ---------------- tests ----------------


def test_is_alive_for_current_process() -> None:
    assert _is_alive(os.getpid()) is True


def test_is_alive_for_dead_pid() -> None:
    # PID 1 always exists on POSIX but signaling it will raise
    # PermissionError, which we treat as alive. Use a guaranteed-dead
    # pid instead: spawn a child, wait, then check after it's gone.
    import subprocess

    p = subprocess.Popen(["true"])
    p.wait()
    # Tiny sleep so the OS reaps before we probe.
    time.sleep(0.05)
    assert _is_alive(p.pid) is False


def test_is_alive_rejects_nonpositive() -> None:
    assert _is_alive(0) is False
    assert _is_alive(-1) is False


def test_status_empty_when_no_pidfile(tmp_controller: DtdObserverController) -> None:
    async def run() -> None:
        with patch.object(
            ctrl_mod, "_last_scanner_event_age", return_value=(None, None)
        ):
            s = await tmp_controller.status()
        assert s.running is False
        assert s.pid is None
        assert s.last_event_at is None

    asyncio.run(run())


def test_status_clears_stale_pidfile(tmp_controller: DtdObserverController) -> None:
    # Write a definitely-dead pid into the pidfile.
    tmp_controller._var_dir.mkdir(parents=True, exist_ok=True)
    tmp_controller._pidfile.write_text("999999\n")

    async def run() -> None:
        with patch.object(
            ctrl_mod, "_last_scanner_event_age", return_value=(None, None)
        ):
            s = await tmp_controller.status()
        assert s.running is False
        # status() should have side-effect-cleared the stale pidfile.
        assert not tmp_controller._pidfile.exists()

    asyncio.run(run())


def test_start_spawns_subprocess_and_writes_pidfile(
    tmp_controller: DtdObserverController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: start spawns the fake script, persists pid, status
    reports running, stop SIGTERMs it and clears the pidfile."""
    # Use the current Python interpreter instead of `uv run python`
    # so the test doesn't need uv set up under tmp_path.
    import subprocess
    import sys

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # Substitute "uv run python <script>" with "<python> <script>"
        # so the real Popen still runs but without the uv shim.
        if cmd[:3] == ["uv", "run", "python"]:
            cmd = [sys.executable] + cmd[3:]
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)

    async def run() -> None:
        with patch.object(
            ctrl_mod, "_last_scanner_event_age", return_value=(None, None)
        ):
            s = await tmp_controller.start()
            assert s.running is True
            assert s.pid is not None
            assert tmp_controller._pidfile.exists()
            assert tmp_controller._pidfile.read_text().strip() == str(s.pid)

            # Second start: idempotent, returns same pid.
            s2 = await tmp_controller.start()
            assert s2.running is True
            assert s2.pid == s.pid

            s3 = await tmp_controller.stop(timeout_seconds=2.0)
            assert s3.running is False
            assert not tmp_controller._pidfile.exists()

    asyncio.run(run())


def test_start_raises_when_script_missing(
    tmp_path: Path,
) -> None:
    c = DtdObserverController(repo_root=tmp_path, var_dir=tmp_path / "var")
    # No scripts/dtd_run.py created.
    async def run() -> None:
        with pytest.raises(FileNotFoundError, match="dtd_run.py"):
            await c.start()

    asyncio.run(run())


def test_stop_is_noop_when_not_running(
    tmp_controller: DtdObserverController,
) -> None:
    async def run() -> None:
        with patch.object(
            ctrl_mod, "_last_scanner_event_age", return_value=(None, None)
        ):
            s = await tmp_controller.stop()
        assert s.running is False

    asyncio.run(run())


def test_stop_kills_unresponsive_process(
    tmp_controller: DtdObserverController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the child ignores SIGTERM, stop() must escalate to SIGKILL
    after the timeout."""
    scripts = tmp_controller._repo_root / "scripts"
    # Replace fake script with one that ignores SIGTERM.
    (scripts / "dtd_run.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ignoring SIGTERM', flush=True)\n"
        "time.sleep(120)\n"
    )

    import subprocess
    import sys

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if cmd[:3] == ["uv", "run", "python"]:
            cmd = [sys.executable] + cmd[3:]
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)

    async def run() -> None:
        with patch.object(
            ctrl_mod, "_last_scanner_event_age", return_value=(None, None)
        ):
            s = await tmp_controller.start()
            assert s.running is True
            pid = s.pid
            assert pid is not None

            # Tight timeout so the test stays fast; SIGKILL escalation
            # should fire within ~1s.
            s2 = await tmp_controller.stop(timeout_seconds=0.5)
            assert s2.running is False
            # Process should be reaped by SIGKILL.
            assert _is_alive(pid) is False

    asyncio.run(run())
