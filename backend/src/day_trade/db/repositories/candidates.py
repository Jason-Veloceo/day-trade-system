"""Candidate read queries used by the API."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from day_trade.db.models import Candidate, FilterEvaluation, News


async def list_candidates(
    session: AsyncSession,
    *,
    trading_day: dt.date | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[Candidate]:
    stmt = select(Candidate).order_by(Candidate.last_alert_at.desc()).limit(limit)
    if trading_day is not None:
        stmt = stmt.where(Candidate.trading_day == trading_day)
    if status is not None:
        stmt = stmt.where(Candidate.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def get_candidate(session: AsyncSession, candidate_id: int) -> Candidate | None:
    stmt = (
        select(Candidate)
        .options(selectinload(Candidate.evaluations))
        .where(Candidate.id == candidate_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_news(session: AsyncSession, newsid: str) -> News | None:
    return (await session.execute(select(News).where(News.newsid == newsid))).scalar_one_or_none()


async def list_evaluations(session: AsyncSession, candidate_id: int) -> list[FilterEvaluation]:
    stmt = select(FilterEvaluation).where(FilterEvaluation.candidate_id == candidate_id)
    return list((await session.execute(stmt)).scalars().all())
