"""FastAPI application factory."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from day_trade.api import auto_arm, candidates, dtd, engine, health, rules, ws
from day_trade.auto_arm import get_worker as get_auto_arm_worker
from day_trade.config import get_settings
from day_trade.db.models import EngineRun
from day_trade.db.session import session_scope

logger = logging.getLogger(__name__)


async def _sweep_orphaned_engine_runs() -> None:
    """Mark any engine_runs left in non-terminal status as stopped.

    Non-terminal statuses (`starting`, `running`, `stopping`) imply an
    engine instance was holding state in memory. If the backend was killed
    ungracefully (uvicorn --reload, SIGKILL, crash) those instances are
    gone but the DB rows look "live" until something rewrites them. This
    sweep runs once on startup and cleans them up so the UI is honest.
    """
    try:
        async with session_scope() as s:
            result = await s.execute(
                update(EngineRun)
                .where(EngineRun.status.in_(("starting", "running", "stopping")))
                .values(
                    status="stopped",
                    stopped_at=dt.datetime.now(dt.timezone.utc),
                    stop_reason="backend_restart_orphaned",
                )
                .execution_options(synchronize_session=False)
            )
            count = result.rowcount or 0
            if count > 0:
                logger.warning(
                    "swept %d orphaned engine_run row(s) to status=stopped "
                    "(reason=backend_restart_orphaned)",
                    count,
                )
    except Exception:
        logger.exception("failed to sweep orphaned engine_runs on startup")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    await _sweep_orphaned_engine_runs()

    # Item 2: scanner-driven auto-arm worker. Polls the candidates
    # table every `auto_arm_poll_seconds` and arms engines on matching
    # widget alerts. The global toggle (`AUTO_ARM_ENABLED`) defaults
    # to False so this is a no-op for operators who haven't opted in.
    # The worker itself always runs so the toggle is responsive at
    # runtime without a restart.
    auto_arm = get_auto_arm_worker()
    await auto_arm.start()
    try:
        yield
    finally:
        await auto_arm.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="day-trade",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Local dev: allow the Next.js frontend on its default (3000) and on
    # an alternate port (3010) used when 3000 is occupied by another app
    # running concurrently on this machine.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3010",
            "http://127.0.0.1:3010",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(candidates.router)
    app.include_router(rules.router)
    app.include_router(engine.router)
    app.include_router(auto_arm.router)
    app.include_router(dtd.router)
    app.include_router(ws.router)

    _ = settings  # touch settings so import-time errors surface here

    return app


app = create_app()
