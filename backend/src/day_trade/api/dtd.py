"""REST API for the DTD observer subprocess controller.

  GET  /dtd/observer/status  -> running / pid / last-event age
  POST /dtd/observer/start   -> spawn dtd_run.py as a detached subprocess
  POST /dtd/observer/stop    -> SIGTERM the observer (SIGKILL after timeout)

These endpoints exist so the engine page can show observer health and
control the process without the operator opening a terminal. The
underlying script (`scripts/dtd_run.py`) is unchanged — we just wrap
its lifecycle in a managed Popen.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from day_trade.dtd_control import get_controller

router = APIRouter(prefix="/dtd/observer", tags=["dtd"])


class DtdObserverStatusOut(BaseModel):
    running: bool
    pid: int | None
    started_at: str | None
    last_event_at: str | None
    last_event_age_seconds: float | None
    log_path: str | None
    pidfile_path: str


@router.get("/status", response_model=DtdObserverStatusOut)
async def status() -> DtdObserverStatusOut:
    s = await get_controller().status()
    return DtdObserverStatusOut(**s.to_dict())


@router.post("/start", response_model=DtdObserverStatusOut)
async def start() -> DtdObserverStatusOut:
    """Start the DTD observer subprocess (idempotent). Returns the
    post-start status. If the script fails to launch (Playwright
    profile missing, etc.) the subprocess will exit immediately and
    the next status() will report `running=False` with the log path
    so the operator can inspect."""
    try:
        s = await get_controller().start()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return DtdObserverStatusOut(**s.to_dict())


@router.post("/stop", response_model=DtdObserverStatusOut)
async def stop() -> DtdObserverStatusOut:
    """Stop the DTD observer subprocess. SIGTERM first, then SIGKILL
    after a 5s timeout. Idempotent: if not running, returns the
    current status without error."""
    s = await get_controller().stop()
    return DtdObserverStatusOut(**s.to_dict())
