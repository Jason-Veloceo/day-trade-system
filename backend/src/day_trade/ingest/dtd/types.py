"""Raw types for the DTD `/alert?widget=...` JSON payload.

Field set verified against the captured Momo response (3,250 events):
    fields: [Time, Symbol, Close Price, Volume Today, Float, Rel Vol - Today,
             Rel Vol - 5 Min, Rel Gap, Rel Gain/Loss, Short Interest, Strategy]

Some events have an optional `news` block joined server-side by DTD; others don't.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DtdNews(BaseModel):
    model_config = ConfigDict(extra="allow")

    newsid: int | str
    datetime: str
    storyurl: str
    headline: str
    ts: int


class DtdField(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    val: Any


class DtdAlert(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str  # e.g. 'US-EDHL'
    widget: str  # e.g. 'Momo', 'Running_Up'
    strategy: str  # e.g. 'Low_Float_Former_Momo_Stock'
    event: str  # 'New High'
    fields: list[DtdField]
    day: int  # YYYYMMDD as int (e.g. 20260612)
    ts: int  # ms epoch
    news: DtdNews | None = None


class DtdAlertResponse(BaseModel):
    """Wrapper response shape from `/alert?widget=Momo`."""

    model_config = ConfigDict(extra="allow")

    count: int
    data: list[DtdAlert] = Field(default_factory=list)
