"""API правил рисков организации (этап 11).

Управление — `/api/admin/risk-rules` (Admin и Superadmin, проверка в `app.services.risk_rules` через
`policies.can_manage_risk_rules`); форма анализа — `/api/risk-rules/available`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminAuth, Auth, AuthContext, Db
from app.models.enums import RiskCategory
from app.schemas.catalog import ConfigRevisionListOut, ConfigRevisionOut
from app.schemas.risk_rules import (
    AvailableRiskRulesOut,
    RiskRuleCreateIn,
    RiskRuleListOut,
    RiskRuleOut,
    RiskRuleUpdateIn,
    RiskRuleVersionIn,
)
from app.services import risk_rules as svc
from app.services.catalog import Actor

router = APIRouter(tags=["risk-rules"])


def _actor(auth: AuthContext) -> Actor:
    return Actor(user=auth.user, ip=auth.ip)


@router.get("/admin/risk-rules")
async def list_rules(
    auth: AdminAuth, db: Db, archived: bool = False, category: RiskCategory | None = None
) -> RiskRuleListOut:
    items = await svc.list_rules(db, _actor(auth), archived=archived, category=category)
    return RiskRuleListOut(items=items, limits=svc.LIMITS)


@router.post("/admin/risk-rules", status_code=201)
async def create_rule(body: RiskRuleCreateIn, auth: AdminAuth, db: Db) -> RiskRuleOut:
    return svc.rule_out(await svc.create_rule(db, _actor(auth), body))


@router.get("/admin/risk-rules/{rule_id}")
async def get_rule(rule_id: uuid.UUID, auth: AdminAuth, db: Db) -> RiskRuleOut:
    return svc.rule_out(await svc.get_rule(db, _actor(auth), rule_id))


@router.patch("/admin/risk-rules/{rule_id}")
async def update_rule(rule_id: uuid.UUID, body: RiskRuleUpdateIn, auth: AdminAuth, db: Db) -> RiskRuleOut:
    return svc.rule_out(await svc.update_rule(db, _actor(auth), rule_id, body))


@router.post("/admin/risk-rules/{rule_id}/archive")
async def archive_rule(rule_id: uuid.UUID, body: RiskRuleVersionIn, auth: AdminAuth, db: Db) -> RiskRuleOut:
    rule = await svc.set_archived(db, _actor(auth), rule_id, body.version, archived=True)
    return svc.rule_out(rule)


@router.post("/admin/risk-rules/{rule_id}/restore")
async def restore_rule(rule_id: uuid.UUID, body: RiskRuleVersionIn, auth: AdminAuth, db: Db) -> RiskRuleOut:
    rule = await svc.set_archived(db, _actor(auth), rule_id, body.version, archived=False)
    return svc.rule_out(rule)


@router.get("/admin/risk-rules/{rule_id}/revisions")
async def rule_revisions(
    rule_id: uuid.UUID, auth: AdminAuth, db: Db, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ConfigRevisionListOut:
    rows = await svc.list_revisions(db, _actor(auth), rule_id, limit)
    return ConfigRevisionListOut(
        items=[ConfigRevisionOut.model_validate(r, from_attributes=True) for r in rows]
    )


@router.get("/risk-rules/available")
async def available_rules(auth: Auth, db: Db) -> AvailableRiskRulesOut:
    return AvailableRiskRulesOut(items=await svc.available(db, auth.user), limits=svc.LIMITS)
