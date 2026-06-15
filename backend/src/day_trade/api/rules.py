"""REST endpoints for editing the active filter rule set."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from day_trade.api.schemas import RuleOut, RuleSetOut, RuleSetUpdateIn
from day_trade.db.repositories import rule_sets as repo
from day_trade.db.session import session_scope
from day_trade.filters.rules import RuleSpec
from day_trade.ws.broker import get_broker
from day_trade.ws.topics import RULE_SET_CHANGED

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("/active", response_model=RuleSetOut)
async def get_active() -> RuleSetOut:
    async with session_scope() as session:
        rule_set, _ = await repo.get_active_rule_set(session)
        if rule_set is None:
            raise HTTPException(404, "no active rule set")
        return RuleSetOut(
            id=rule_set.id,
            name=rule_set.name,
            is_active=rule_set.is_active,
            created_at=rule_set.created_at,
            note=rule_set.note,
            rules=[RuleOut.model_validate(r) for r in rule_set.rules],
        )


@router.put("/active", response_model=RuleSetOut, status_code=201)
async def replace_active(payload: RuleSetUpdateIn) -> RuleSetOut:
    """Create a new rule set version and mark it active.

    Old versions remain in the DB so we can attribute outcomes to specific
    versions later.
    """
    specs = [
        RuleSpec(
            rule_key=r.rule_key,
            field=r.field,
            op=r.op,
            value=r.value,
            enabled=r.enabled,
            severity=r.severity,
        )
        for r in payload.rules
    ]
    async with session_scope() as session:
        rule_set = await repo.create_rule_set(
            session, name=payload.name, rules=specs, make_active=True, note=payload.note
        )
        await session.refresh(rule_set, attribute_names=["rules"])
        result = RuleSetOut(
            id=rule_set.id,
            name=rule_set.name,
            is_active=rule_set.is_active,
            created_at=rule_set.created_at,
            note=rule_set.note,
            rules=[RuleOut.model_validate(r) for r in rule_set.rules],
        )
        await get_broker().publish(RULE_SET_CHANGED, {"rule_set_id": rule_set.id})
        return result
    raise RuntimeError("session_scope yielded nothing")
