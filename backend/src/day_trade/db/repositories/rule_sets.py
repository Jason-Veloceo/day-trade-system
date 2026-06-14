"""Filter rule-set queries (CRUD + active-set fetch).

Used by both the ingestion pipeline (to load the active rules for evaluation)
and the API (to let the user edit them).
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from day_trade.db.models import FilterRule, FilterRuleSet
from day_trade.filters.rules import RuleSpec


def _row_to_spec(row: FilterRule) -> RuleSpec:
    return RuleSpec(
        rule_key=row.rule_key,
        field=row.field,
        op=row.op,
        value=row.value,
        enabled=row.enabled,
        severity=row.severity,
    )


async def get_active_rule_set(session: AsyncSession) -> tuple[FilterRuleSet | None, list[RuleSpec]]:
    """Return (active set, rules as RuleSpecs). If no active set exists, returns (None, [])."""
    stmt = (
        select(FilterRuleSet)
        .options(selectinload(FilterRuleSet.rules))
        .where(FilterRuleSet.is_active.is_(True))
        .limit(1)
    )
    rule_set = (await session.execute(stmt)).scalar_one_or_none()
    if rule_set is None:
        return None, []
    return rule_set, [_row_to_spec(r) for r in rule_set.rules]


async def create_rule_set(
    session: AsyncSession, *, name: str, rules: list[RuleSpec], make_active: bool, note: str | None = None
) -> FilterRuleSet:
    """Create a new rule set. If make_active, deactivates any existing active set first."""
    if make_active:
        await session.execute(update(FilterRuleSet).values(is_active=False).where(FilterRuleSet.is_active.is_(True)))

    rule_set = FilterRuleSet(name=name, is_active=make_active, note=note)
    session.add(rule_set)
    await session.flush()

    for spec in rules:
        session.add(
            FilterRule(
                rule_set_id=rule_set.id,
                rule_key=spec.rule_key,
                field=spec.field,
                op=spec.op,
                value=spec.value,
                enabled=spec.enabled,
                severity=spec.severity,
            )
        )
    await session.flush()
    return rule_set


async def ensure_default_rule_set(session: AsyncSession, *, name: str, rules: list[RuleSpec]) -> FilterRuleSet:
    """Idempotent: if no active set exists, create one with the given defaults."""
    existing, _ = await get_active_rule_set(session)
    if existing is not None:
        return existing
    return await create_rule_set(session, name=name, rules=rules, make_active=True, note="seeded defaults")
