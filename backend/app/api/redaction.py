"""API маскировки перед облачным анализом (этап 10).

- `/admin/redaction` — настройки: читают Admin и Superadmin, меняет только Superadmin.
- `/redaction/config` — категории и разрешение отправки без маскировки (любой пользователь).
- `/documents/{id}/versions/{vid}/redaction-previews` — создать предпросмотр (право анализа документа).
- `/redaction-previews/{id}` — чтение, правки, подтверждение: только автору
  (`scoped.get_redaction_preview_for`), чужой и несуществующий неразличимы — 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from app.api.deps import AdminAuth, Auth, Db
from app.redaction.settings import RedactionSettings
from app.schemas.redaction import (
    PreviewConfirmIn,
    PreviewEditIn,
    PreviewOut,
    RedactionConfigOut,
    RedactionSettingsIn,
    RedactionSettingsOut,
)
from app.security import policies
from app.security.scoped import get_redaction_preview_for
from app.security.secretbox import SecretBox
from app.services import redaction as svc

router = APIRouter(tags=["redaction"])


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


async def _settings_out(db: Db, auth: AdminAuth) -> RedactionSettingsOut:
    state = await svc.get_state(db)
    await db.commit()
    return RedactionSettingsOut(
        settings=state.settings,
        revision=state.revision,
        updated_at=state.updated_at,
        updated_by_id=state.updated_by_id,
        can_manage=policies.can_manage_redaction(auth.user),
        categories=svc.categories_out(),
    )


@router.get("/admin/redaction")
async def get_redaction_settings(auth: AdminAuth, db: Db) -> RedactionSettingsOut:
    return await _settings_out(db, auth)


@router.put("/admin/redaction")
async def put_redaction_settings(body: RedactionSettingsIn, auth: AdminAuth, db: Db) -> RedactionSettingsOut:
    new = RedactionSettings.model_validate(body.model_dump(exclude={"confirm_allow_unmasked"}))
    await svc.update_settings(db, auth.user, auth.ip, new, confirm_allow_unmasked=body.confirm_allow_unmasked)
    return await _settings_out(db, auth)


@router.get("/redaction/config")
async def get_redaction_config(auth: Auth, db: Db) -> RedactionConfigOut:
    _ = auth
    state = await svc.get_state(db)
    await db.commit()
    return RedactionConfigOut(
        allow_unmasked=state.settings.allow_unmasked,
        unmasked_notice_version=svc.UNMASKED_NOTICE_VERSION,
        preview_ttl_hours=state.settings.preview_ttl_hours,
        categories=svc.categories_out(),
    )


@router.post("/documents/{document_id}/versions/{version_id}/redaction-previews", status_code=201)
async def create_preview(
    document_id: uuid.UUID, version_id: uuid.UUID, request: Request, auth: Auth, db: Db
) -> PreviewOut:
    box = _box(request)
    preview = await svc.create_preview(db, auth.user, box, document_id=document_id, version_id=version_id)
    return await svc.preview_view(db, box, preview)


@router.get("/redaction-previews/{preview_id}")
async def get_preview(preview_id: uuid.UUID, request: Request, auth: Auth, db: Db) -> PreviewOut:
    preview = await get_redaction_preview_for(db, auth.user, preview_id)
    return await svc.preview_view(db, _box(request), preview)


@router.patch("/redaction-previews/{preview_id}")
async def edit_preview(
    preview_id: uuid.UUID, body: PreviewEditIn, request: Request, auth: Auth, db: Db
) -> PreviewOut:
    box = _box(request)
    preview = await get_redaction_preview_for(db, auth.user, preview_id, for_update=True)
    preview = await svc.edit_preview(db, box, preview, body.ops)
    return await svc.preview_view(db, box, preview)


@router.post("/redaction-previews/{preview_id}/confirm")
async def confirm_preview(
    preview_id: uuid.UUID, body: PreviewConfirmIn, request: Request, auth: Auth, db: Db
) -> PreviewOut:
    preview = await get_redaction_preview_for(db, auth.user, preview_id, for_update=True)
    preview = await svc.confirm_preview(db, auth.user, auth.ip, preview, body)
    return await svc.preview_view(db, _box(request), preview)
