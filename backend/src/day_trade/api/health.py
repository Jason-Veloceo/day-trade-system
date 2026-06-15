"""Liveness + readiness."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from day_trade.db.session import session_scope

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    async with session_scope() as session:
        await session.execute(text("SELECT 1"))
        return {"status": "ready", "db": "ok"}
