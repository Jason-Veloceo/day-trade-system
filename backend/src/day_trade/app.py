"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from day_trade.api import candidates, engine, health, rules, ws
from day_trade.config import get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="day-trade",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Local dev: allow the Next.js frontend on a different port.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(candidates.router)
    app.include_router(rules.router)
    app.include_router(engine.router)
    app.include_router(ws.router)

    _ = settings  # touch settings so import-time errors surface here

    return app


app = create_app()
