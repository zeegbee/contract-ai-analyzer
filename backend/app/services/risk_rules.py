"""Правила рисков организации (этап 11, docs/RISK_RULES_PLAN.md).

- Управляют Admin и Superadmin (`policies.can_manage_risk_rules`); остальные видят только включённые
  правила в форме анализа (`available`).
- Изменения — с оптимистической блокировкой (`version`) и записью `ConfigRevision`
  (`entity_type="risk_rule"`). В аудит — только код правила и имена изменённых полей, без текста.
- Правила не удаляются, а архивируются: анализы ссылаются на текст правила через snapshot.
- При запуске анализа выбранные правила проверяются и копируются в snapshot (`snapshot_rules`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.prompts import CHUNK_RULE_KINDS, CROSS_RULE_KINDS, org_rules_block, rules_for
from app.errors import AppError, ConflictError, NotFoundError
from app.models.enums import RiskCategory
from app.models.risk_rules import RISK_RULE_CODE_SEQ, RiskRule
from app.models.system import ConfigRevision
from app.models.users import User
from app.schemas.risk_rules import (
    TEXT_MAX,
    AvailableRiskRuleOut,
    RiskRuleCreateIn,
    RiskRuleLimitsOut,
    RiskRuleOut,
    RiskRuleUpdateIn,
)
from app.security import policies
from app.services.audit import AuditAction, record
from app.services.catalog import Actor, _add_revision, _check_version, _diff

MAX_RULES_CATALOG = 100
MAX_RULES_PER_ANALYSIS = 20
MAX_RULES_PROMPT_CHARS = 8000
LIMITS = RiskRuleLimitsOut(
    catalog=MAX_RULES_CATALOG,
    per_analysis=MAX_RULES_PER_ANALYSIS,
    text_chars=TEXT_MAX,
    prompt_chars=MAX_RULES_PROMPT_CHARS,
)
_FIELDS = (
    "title",
    "category",
    "kind",
    "description",
    "signs",
    "severity",
    "recommendation",
    "enabled",
    "default_selected",
)
# Поля, которые можно сбросить в null.
_NULLABLE = frozenset({"signs", "severity", "recommendation"})


def _now() -> datetime:
    return datetime.now(UTC)


def rule_out(r: RiskRule) -> RiskRuleOut:
    return RiskRuleOut.model_validate(r, from_attributes=True)


def _state(r: RiskRule) -> dict[str, Any]:
    state = {name: getattr(r, name) for name in _FIELDS}
    state["archived"] = r.archived_at is not None
    return state


async def _get(db: AsyncSession, rule_id: uuid.UUID, *, for_update: bool = False) -> RiskRule:
    stmt = select(RiskRule).where(RiskRule.id == rule_id)
    if for_update:
        stmt = stmt.with_for_update()
    rule = (await db.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Правило не найдено", code="risk_rule_not_found")
    return rule


async def _active_count(db: AsyncSession) -> int:
    stmt = select(func.count()).select_from(RiskRule).where(RiskRule.archived_at.is_(None))
    return int((await db.execute(stmt)).scalar_one())


async def _check_catalog_limit(db: AsyncSession) -> None:
    # Сериализация создания и восстановления: лимит не обходится параллельными запросами.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext("risk-rules-catalog"))))
    if await _active_count(db) >= MAX_RULES_CATALOG:
        raise ConflictError(
            f"В каталоге уже {MAX_RULES_CATALOG} правил. Отправьте ненужные правила в архив.",
            code="risk_rules_catalog_full",
            details={"limit": MAX_RULES_CATALOG},
        )


def _audit(
    db: AsyncSession, actor: Actor, action: AuditAction, rule: RiskRule, fields: Sequence[str] = ()
) -> None:
    details: dict[str, Any] = {"code": rule.code}
    if fields:
        details["fields"] = sorted(fields)
    record(
        db,
        action,
        actor_id=actor.user.id,
        target_type="risk_rule",
        target_id=rule.id,
        ip=actor.ip,
        details=details,
    )


async def list_rules(
    db: AsyncSession, actor: Actor, *, archived: bool = False, category: RiskCategory | None = None
) -> list[RiskRuleOut]:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    stmt = select(RiskRule).where(
        RiskRule.archived_at.is_not(None) if archived else RiskRule.archived_at.is_(None)
    )
    if category is not None:
        stmt = stmt.where(RiskRule.category == category)
    rows = (await db.execute(stmt.order_by(RiskRule.code))).scalars()
    return [rule_out(r) for r in rows]


async def get_rule(db: AsyncSession, actor: Actor, rule_id: uuid.UUID) -> RiskRule:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    return await _get(db, rule_id)


async def create_rule(db: AsyncSession, actor: Actor, body: RiskRuleCreateIn) -> RiskRule:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    await _check_catalog_limit(db)
    number = int((await db.execute(select(RISK_RULE_CODE_SEQ.next_value()))).scalar_one())
    rule = RiskRule(
        id=uuid.uuid4(),
        code=f"RR-{number:03d}",
        **body.model_dump(),
        created_by_id=actor.user.id,
        updated_by_id=actor.user.id,
    )
    db.add(rule)
    await db.flush()
    _add_revision(db, ("risk_rule", rule.id, rule.version), _diff({}, _state(rule)), actor)
    _audit(db, actor, AuditAction.risk_rule_created, rule)
    await db.commit()
    await db.refresh(rule)
    return rule


def _update_action(diff: dict[str, Any]) -> AuditAction:
    if set(diff) == {"enabled"}:
        return AuditAction.risk_rule_enabled if diff["enabled"]["to"] else AuditAction.risk_rule_disabled
    return AuditAction.risk_rule_updated


async def update_rule(db: AsyncSession, actor: Actor, rule_id: uuid.UUID, body: RiskRuleUpdateIn) -> RiskRule:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    rule = await _get(db, rule_id, for_update=True)
    _check_version(rule.version, body.version)
    if rule.archived_at is not None:
        raise ConflictError("Правило в архиве — сначала восстановите его", code="risk_rule_archived")
    before = _state(rule)
    for name in body.model_fields_set - {"version"}:
        value = getattr(body, name)
        if value is None and name not in _NULLABLE:
            raise AppError("Поле не может быть пустым", code="validation_error", status_code=422)
        setattr(rule, name, value)
    diff = _diff(before, _state(rule))
    if not diff:
        await db.commit()
        await db.refresh(rule)
        return rule
    rule.updated_by_id = actor.user.id
    await db.flush()
    _add_revision(db, ("risk_rule", rule.id, rule.version), diff, actor)
    _audit(db, actor, _update_action(diff), rule, list(diff))
    await db.commit()
    await db.refresh(rule)
    return rule


async def set_archived(
    db: AsyncSession, actor: Actor, rule_id: uuid.UUID, version: int, *, archived: bool
) -> RiskRule:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    if not archived:
        await _check_catalog_limit(db)
    rule = await _get(db, rule_id, for_update=True)
    _check_version(rule.version, version)
    if (rule.archived_at is not None) == archived:
        await db.commit()
        await db.refresh(rule)
        return rule
    before = _state(rule)
    rule.archived_at = _now() if archived else None
    rule.updated_by_id = actor.user.id
    await db.flush()
    _add_revision(db, ("risk_rule", rule.id, rule.version), _diff(before, _state(rule)), actor)
    _audit(db, actor, AuditAction.risk_rule_archived if archived else AuditAction.risk_rule_restored, rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def list_revisions(
    db: AsyncSession, actor: Actor, rule_id: uuid.UUID, limit: int
) -> list[ConfigRevision]:
    policies.ensure(policies.can_manage_risk_rules(actor.user))
    await _get(db, rule_id)
    stmt = (
        select(ConfigRevision)
        .where(ConfigRevision.entity_type == "risk_rule", ConfigRevision.entity_id == str(rule_id))
        .order_by(ConfigRevision.revision.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars())


# ------------------------------------------------------------------ форма анализа и snapshot


def _usable() -> Any:
    return RiskRule.enabled.is_(True) & RiskRule.archived_at.is_(None)


async def available(db: AsyncSession, user: User) -> list[AvailableRiskRuleOut]:
    """Включённые неархивные правила — для всех, кто может запускать анализ (иначе пустой список)."""
    if not policies.can_upload_documents(user):
        return []
    rows = (await db.execute(select(RiskRule).where(_usable()).order_by(RiskRule.code))).scalars()
    return [AvailableRiskRuleOut.model_validate(r, from_attributes=True) for r in rows]


def snapshot_entry(rule: RiskRule) -> dict[str, Any]:
    """Копия правила в snapshot анализа: анализ и его повтор не зависят от последующих изменений."""
    return {
        "id": str(rule.id),
        "code": rule.code,
        "revision": rule.version,
        "title": rule.title,
        "category": rule.category.value,
        "kind": rule.kind.value,
        "description": rule.description,
        "signs": rule.signs,
        "severity": rule.severity.value if rule.severity else None,
        "recommendation": rule.recommendation,
    }


def _rule_error(message: str, code: str, **details: Any) -> AppError:
    return AppError(message, code=code, status_code=422, details=details or None)


def prompt_sizes(rules: Sequence[dict[str, Any]], categories: Sequence[RiskCategory]) -> tuple[int, int]:
    """Длина блока правил в промпте анализа фрагментов и межраздельного прохода (0 — блока нет)."""
    chunk = rules_for(rules, categories, CHUNK_RULE_KINDS)
    cross = rules_for(rules, categories, CROSS_RULE_KINDS)
    return (
        len(org_rules_block(chunk)) if chunk else 0,
        len(org_rules_block(cross)) if cross else 0,
    )


async def resolve_for_analysis(
    db: AsyncSession, rule_ids: Sequence[uuid.UUID], categories: Sequence[RiskCategory]
) -> list[dict[str, Any]]:
    """Проверяет выбранные правила и возвращает их snapshot (в порядке кодов). Ошибки — 422."""
    if not rule_ids:
        return []
    unique = list(dict.fromkeys(rule_ids))
    if len(unique) > MAX_RULES_PER_ANALYSIS:
        raise _rule_error(
            f"В одном анализе — не больше {MAX_RULES_PER_ANALYSIS} правил",
            "risk_rules_limit_exceeded",
            limit=MAX_RULES_PER_ANALYSIS,
        )
    stmt = select(RiskRule).where(RiskRule.id.in_(unique), _usable()).order_by(RiskRule.code)
    rows = list((await db.execute(stmt)).scalars())
    if len(rows) != len(unique):
        raise _rule_error(
            "Часть выбранных правил выключена или отправлена в архив. Обновите страницу.",
            "risk_rule_unavailable",
        )
    selected = set(categories)
    outside = [r.code for r in rows if r.category not in selected]
    if outside:
        raise _rule_error(
            "Правило можно применить только вместе с его категорией",
            "risk_rule_category_not_selected",
            rules=outside,
        )
    entries = [snapshot_entry(r) for r in rows]
    if max(prompt_sizes(entries, categories)) > MAX_RULES_PROMPT_CHARS:
        raise _rule_error(
            f"Текст выбранных правил длиннее {MAX_RULES_PROMPT_CHARS} символов. Выберите меньше правил.",
            "risk_rules_too_long",
            limit=MAX_RULES_PROMPT_CHARS,
        )
    return entries
