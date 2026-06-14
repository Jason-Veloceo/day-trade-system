"""Async SQLAlchemy engine + session factory."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import lru_cache
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from day_trade.config import get_settings


def _json_default(obj: Any) -> Any:
    """Handle types stdlib json doesn't know: Decimal, datetime, date."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_serializer(obj: Any) -> str:
    return orjson.dumps(
        obj, default=_json_default, option=orjson.OPT_NON_STR_KEYS
    ).decode("utf-8")


def _json_deserializer(s: str) -> Any:
    return orjson.loads(s)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        json_serializer=_json_serializer,
        json_deserializer=_json_deserializer,
    )


@lru_cache(maxsize=1)
def _session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Use as an async context: `async with session_scope() as s:`.

    Commits on exit, rolls back on exception.
    """
    session = _session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return _session_factory()
