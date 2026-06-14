"""REST endpoints for candidates."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query

from day_trade.api.schemas import CandidateDetailOut, CandidateOut, FilterEvaluationOut
from day_trade.db.repositories import candidates as repo
from day_trade.db.session import session_scope

router = APIRouter(prefix="/candidates", tags=["candidates"])


@router.get("", response_model=list[CandidateOut])
async def list_candidates(
    status: str | None = Query(default=None, description="passed | failed_filter | stale"),
    trading_day: dt.date | None = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=200, le=1000),
) -> list[CandidateOut]:
    async for session in session_scope():
        rows = await repo.list_candidates(
            session, trading_day=trading_day, status=status, limit=limit
        )
        return [CandidateOut.model_validate(r) for r in rows]
    raise RuntimeError("session_scope yielded nothing")


@router.get("/{candidate_id}", response_model=CandidateDetailOut)
async def get_candidate(candidate_id: int) -> CandidateDetailOut:
    async for session in session_scope():
        row = await repo.get_candidate(session, candidate_id)
        if row is None:
            raise HTTPException(404, "candidate not found")

        news_headline = news_storyurl = None
        news_datetime: dt.datetime | None = None
        if row.latest_newsid:
            news = await repo.get_news(session, row.latest_newsid)
            if news:
                news_headline = news.headline
                news_storyurl = news.storyurl
                news_datetime = news.datetime_

        return CandidateDetailOut(
            **CandidateOut.model_validate(row).model_dump(),
            evaluations=[FilterEvaluationOut.model_validate(e) for e in row.evaluations],
            news_headline=news_headline,
            news_storyurl=news_storyurl,
            news_datetime=news_datetime,
        )
    raise RuntimeError("session_scope yielded nothing")
