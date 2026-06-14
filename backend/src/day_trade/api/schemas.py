"""Pydantic response schemas exposed by the REST API."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    trading_day: dt.date
    first_alert_at: dt.datetime
    last_alert_at: dt.datetime
    cooldown_until: dt.datetime
    alert_count: int
    widgets_fired: list[str]
    strategies_fired: list[str]
    is_5_pillars: bool

    last_close_price: Decimal | None
    last_volume: int | None
    last_float: int | None
    last_rel_vol_today: Decimal | None
    last_rel_vol_5min: Decimal | None
    last_rel_gap: Decimal | None
    last_rel_gain: Decimal | None
    last_short_interest: int | None

    has_news: bool
    latest_newsid: str | None

    status: str
    failed_rules: list[str]


class FilterEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_key: str
    passed: bool
    observed: Any
    threshold: Any


class CandidateDetailOut(CandidateOut):
    evaluations: list[FilterEvaluationOut] = Field(default_factory=list)
    news_headline: str | None = None
    news_storyurl: str | None = None
    news_datetime: dt.datetime | None = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_key: str
    field: str
    op: str
    value: Any
    enabled: bool
    severity: str
    note: str | None = None


class RuleSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_active: bool
    created_at: dt.datetime
    note: str | None = None
    rules: list[RuleOut]


class RuleUpdateIn(BaseModel):
    rule_key: str
    field: str
    op: str
    value: Any
    enabled: bool = True
    severity: str = "hard"
    note: str | None = None


class RuleSetUpdateIn(BaseModel):
    name: str
    rules: list[RuleUpdateIn]
    note: str | None = None
