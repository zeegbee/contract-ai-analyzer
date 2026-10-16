"""API провайдеров, моделей и настроек облака.

Права — в `app.services.catalog` через `app.security.policies`: модели — Admin и Superadmin,
провайдеры и глобальные настройки — только Superadmin. Секреты в ответах не возвращаются.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.api.deps import AdminAuth, Auth, AuthContext, Db
from app.llm.factory import ProviderFactory
from app.schemas.catalog import (
    AvailableModelsOut,
    CloudConsentIn,
    CloudConsentOut,
    ConfigRevisionListOut,
    ConfigRevisionOut,
    ImportModelsIn,
    LlmSettingsIn,
    LlmSettingsOut,
    ModelCreateIn,
    ModelListOut,
    ModelOut,
    ModelPurpose,
    ModelTestIn,
    ModelTestOut,
    ModelUpdateIn,
    ProviderCheckOut,
    ProviderCreateIn,
    ProviderListOut,
    ProviderOut,
    ProviderUpdateIn,
    RemoteModelListOut,
)
from app.security import policies
from app.security.secretbox import SecretBox
from app.services import catalog as svc

router = APIRouter(tags=["models"])


def _actor(auth: AuthContext) -> svc.Actor:
    return svc.Actor(user=auth.user, ip=auth.ip)


def _box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def _factory(request: Request) -> ProviderFactory:
    factory: ProviderFactory = request.app.state.llm_factory
    return factory


# ------------------------------------------------------------------ провайдеры


@router.get("/admin/providers")
async def list_providers(auth: AdminAuth, db: Db) -> ProviderListOut:
    return ProviderListOut(items=await svc.list_providers(db, _actor(auth)))


@router.post("/admin/providers", status_code=201)
async def create_provider(body: ProviderCreateIn, request: Request, auth: AdminAuth, db: Db) -> ProviderOut:
    provider = await svc.create_provider(db, _actor(auth), _box(request), body)
    return await svc.provider_view(db, provider)


@router.get("/admin/providers/{provider_id}")
async def get_provider(provider_id: uuid.UUID, auth: AdminAuth, db: Db) -> ProviderOut:
    policies.ensure(policies.can_manage_models(auth.user))
    return await svc.provider_view(db, await svc.get_provider(db, provider_id))


@router.patch("/admin/providers/{provider_id}")
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdateIn,
    request: Request,
    response: Response,
    auth: AdminAuth,
    db: Db,
) -> ProviderOut:
    provider, secret_reset = await svc.update_provider(db, _actor(auth), _box(request), provider_id, body)
    if secret_reset:
        # Клиенту сообщаем, что ключ нужно ввести заново (адрес изменился).
        response.headers["X-Secret-Reset"] = "1"
    return await svc.provider_view(db, provider)


@router.delete("/admin/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: uuid.UUID, auth: AdminAuth, db: Db) -> Response:
    await svc.delete_provider(db, _actor(auth), provider_id)
    return Response(status_code=204)


@router.post("/admin/providers/{provider_id}/check")
async def check_provider(
    provider_id: uuid.UUID, request: Request, auth: AdminAuth, db: Db
) -> ProviderCheckOut:
    provider, health = await svc.check_provider(db, _actor(auth), _factory(request), provider_id)
    return ProviderCheckOut(provider=await svc.provider_view(db, provider), health=health)


@router.get("/admin/providers/{provider_id}/remote-models")
async def remote_models(
    provider_id: uuid.UUID,
    request: Request,
    auth: AdminAuth,
    db: Db,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> RemoteModelListOut:
    items = await svc.remote_models(db, _actor(auth), _factory(request), provider_id, q)
    return RemoteModelListOut(items=items[:limit], total=len(items), fetched_at=datetime.now(UTC))


@router.post("/admin/providers/{provider_id}/import-models", status_code=201)
async def import_models(
    provider_id: uuid.UUID, body: ImportModelsIn, request: Request, auth: AdminAuth, db: Db
) -> ModelListOut:
    created = await svc.import_models(
        db, _actor(auth), _factory(request), provider_id, body.external_model_ids
    )
    provider = await svc.get_provider(db, provider_id)
    return ModelListOut(items=[svc.model_out(m, provider) for m in created])


# ------------------------------------------------------------------ модели


@router.get("/admin/models")
async def list_models(auth: AdminAuth, db: Db) -> ModelListOut:
    return ModelListOut(items=await svc.list_models(db, _actor(auth)))


@router.post("/admin/models", status_code=201)
async def create_model(body: ModelCreateIn, auth: AdminAuth, db: Db) -> ModelOut:
    model = await svc.create_model(db, _actor(auth), body)
    return svc.model_out(*await svc.get_model(db, model.id))


@router.get("/admin/models/{model_id}")
async def get_model(model_id: uuid.UUID, auth: AdminAuth, db: Db) -> ModelOut:
    policies.ensure(policies.can_manage_models(auth.user))
    return svc.model_out(*await svc.get_model(db, model_id))


@router.patch("/admin/models/{model_id}")
async def update_model(model_id: uuid.UUID, body: ModelUpdateIn, auth: AdminAuth, db: Db) -> ModelOut:
    model = await svc.update_model(db, _actor(auth), model_id, body)
    return svc.model_out(*await svc.get_model(db, model.id))


@router.delete("/admin/models/{model_id}", status_code=204)
async def delete_model(model_id: uuid.UUID, auth: AdminAuth, db: Db) -> Response:
    await svc.delete_model(db, _actor(auth), model_id)
    return Response(status_code=204)


@router.post("/admin/models/{model_id}/test")
async def test_model(
    model_id: uuid.UUID, body: ModelTestIn, request: Request, auth: AdminAuth, db: Db
) -> ModelTestOut:
    model, provider, ok, steps = await svc.test_model(
        db, _actor(auth), _factory(request), model_id, generate=body.generate
    )
    return ModelTestOut(model=svc.model_out(model, provider), ok=ok, steps=steps)


@router.get("/admin/config-revisions")
async def list_revisions(
    auth: AdminAuth,
    db: Db,
    entity_type: Annotated[str | None, Query(pattern="^(provider|model|setting)$")] = None,
    entity_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConfigRevisionListOut:
    rows = await svc.list_revisions(db, _actor(auth), entity_type, entity_id, limit)
    return ConfigRevisionListOut(
        items=[ConfigRevisionOut.model_validate(r, from_attributes=True) for r in rows]
    )


# ------------------------------------------------------------------ настройки облака


async def _settings_out(db: Db, auth: AuthContext) -> LlmSettingsOut:
    return LlmSettingsOut(
        cloud_models_enabled=await svc.cloud_models_enabled(db),
        cloud_notice=await svc.cloud_notice(db),
        can_edit=policies.can_manage_llm_settings(auth.user),
    )


@router.get("/admin/llm-settings")
async def get_llm_settings(auth: AdminAuth, db: Db) -> LlmSettingsOut:
    return await _settings_out(db, auth)


@router.put("/admin/llm-settings")
async def update_llm_settings(body: LlmSettingsIn, auth: AdminAuth, db: Db) -> LlmSettingsOut:
    await svc.update_llm_settings(
        db, _actor(auth), enabled=body.cloud_models_enabled, notice_text=body.cloud_notice_text
    )
    return await _settings_out(db, auth)


# ------------------------------------------------------------------ выбор модели пользователем


@router.get("/models/available")
async def available_models(
    auth: Auth, db: Db, purpose: Annotated[ModelPurpose, Query()] = ModelPurpose.analysis
) -> AvailableModelsOut:
    items, cloud_enabled, notice = await svc.available_models(db, auth.user, purpose)
    return AvailableModelsOut(items=items, cloud_models_enabled=cloud_enabled, cloud_notice=notice)


@router.post("/models/{model_id}/cloud-consent", status_code=201)
async def cloud_consent(model_id: uuid.UUID, body: CloudConsentIn, auth: Auth, db: Db) -> CloudConsentOut:
    consent = await svc.record_cloud_consent(db, _actor(auth), model_id, body.notice_version)
    return CloudConsentOut(
        provider_config_id=consent.provider_config_id,
        notice_version=consent.notice_version,
        accepted_at=consent.accepted_at,
    )
