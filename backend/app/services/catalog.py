"""Каталог провайдеров и моделей, настройки облака, доступность моделей и snapshot конфигурации.

Правила:
- права проверяются здесь (через `app.security.policies`) для каждого изменения;
- секрет провайдера шифруется (`SecretBox`) и никогда не возвращается; в ревизиях и аудите — `***`;
- изменения провайдеров/моделей — с оптимистической блокировкой (`version`) и записью `ConfigRevision`;
- HTTP-запросы к провайдерам выполняются вне транзакции БД (сначала commit чтения).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ConflictError, NotFoundError, PermissionDenied
from app.llm.base import ChatMessage, GenerationParams, RemoteModelInfo, StructuredRequest
from app.llm.errors import BlockedDestination, InvalidModelOutput, ProviderError
from app.llm.factory import PROVIDER_TITLES, ProviderFactory, network_policy
from app.llm.netguard import parse_base_url, url_warnings
from app.llm.ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_URL
from app.llm.openrouter import DEFAULT_BASE_URL as OPENROUTER_DEFAULT_URL
from app.llm.proxyapi import DEFAULT_BASE_URL as PROXYAPI_DEFAULT_URL
from app.models.analyses import Analysis
from app.models.catalog import ModelConfig, ProviderConfig
from app.models.enums import KEYED_CLOUD_PROVIDERS, ProviderType, StructuredOutputSupport, UserRole
from app.models.system import ConfigRevision, SystemSetting
from app.models.users import ConsentRecord, User, UserModelPermission
from app.schemas.catalog import (
    AvailableModelOut,
    CloudNoticeOut,
    GenerationParamsIn,
    HealthOut,
    ModelCreateIn,
    ModelOut,
    ModelPurpose,
    ModelUpdateIn,
    PricingIn,
    PricingOut,
    ProviderCreateIn,
    ProviderOut,
    ProviderUpdateIn,
    RemoteModelOut,
    TestStep,
)
from app.security import policies
from app.security.secretbox import SecretBox
from app.services.audit import AuditAction, record

CLOUD_ENABLED_KEY = "cloud_models_enabled"
CLOUD_NOTICE_KEY = "cloud_notice"
SECRET_MASK = "***"  # noqa: S105 - маска, не секрет

DEFAULT_CLOUD_NOTICE = (
    "Выбрана облачная модель. Фрагменты текста договора будут переданы через интернет внешнему "
    "провайдеру (OpenRouter, ProxyAPI) и далее компании, которая обслуживает выбранную модель. "
    "Условия хранения и использования переданных данных определяет провайдер — проверьте их до отправки "
    "конфиденциальных документов. Если передача недопустима, выберите локальную модель. "
    "Результат анализа не заменяет консультацию юриста."
)

_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-type",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "expect",
    }
)
_SECRETISH_HEADER = re.compile(r"(key|token|secret|auth|pass|cookie|session)", re.IGNORECASE)


@dataclass
class Actor:
    user: User
    ip: str | None


# ------------------------------------------------------------------ вспомогательное


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {"from": _jsonable(before.get(key)), "to": _jsonable(after.get(key))}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def _add_revision(
    db: AsyncSession, entity: tuple[str, uuid.UUID | str, int], diff: dict[str, Any], actor: Actor
) -> None:
    entity_type, entity_id, revision = entity
    db.add(
        ConfigRevision(
            entity_type=entity_type,
            entity_id=str(entity_id),
            revision=revision,
            diff=diff,
            changed_by_id=actor.user.id,
        )
    )


def _check_version(current: int, expected: int) -> None:
    if current != expected:
        raise ConflictError(
            "Запись изменена другим пользователем. Обновите страницу и повторите.", code="version_conflict"
        )


def _blocked_to_app_error(exc: BlockedDestination) -> AppError:
    return AppError(exc.message, code=exc.code, status_code=422)


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ настройки облака


async def _setting(db: AsyncSession, key: str) -> Any:
    return (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == key))
    ).scalar_one_or_none()


async def cloud_models_enabled(db: AsyncSession) -> bool:
    # По умолчанию облачные модели выключены: Superadmin включает их явно.
    return (await _setting(db, CLOUD_ENABLED_KEY)) is True


async def cloud_notice(db: AsyncSession) -> CloudNoticeOut:
    value = await _setting(db, CLOUD_NOTICE_KEY)
    if (
        isinstance(value, dict)
        and isinstance(value.get("version"), str)
        and isinstance(value.get("text"), str)
    ):
        return CloudNoticeOut(version=value["version"], text=value["text"])
    # Версия 2: в тексте названы оба облачных агрегатора (OpenRouter, ProxyAPI) — согласия
    # запрашиваются заново.
    return CloudNoticeOut(version="2", text=DEFAULT_CLOUD_NOTICE)


async def _put_setting(db: AsyncSession, key: str, value: Any, actor: Actor) -> None:
    await db.execute(
        insert(SystemSetting)
        .values(key=key, value=value, updated_by_id=actor.user.id)
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": value, "updated_by_id": actor.user.id, "updated_at": func.now()},
        )
    )


async def update_llm_settings(
    db: AsyncSession, actor: Actor, *, enabled: bool | None, notice_text: str | None
) -> None:
    policies.ensure(policies.can_manage_llm_settings(actor.user))
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('llm_settings'))"))
    before_enabled = await cloud_models_enabled(db)
    before_notice = await cloud_notice(db)
    diff: dict[str, Any] = {}
    if enabled is not None and enabled != before_enabled:
        await _put_setting(db, CLOUD_ENABLED_KEY, enabled, actor)
        diff[CLOUD_ENABLED_KEY] = {"from": before_enabled, "to": enabled}
    if notice_text is not None and notice_text != before_notice.text:
        # Новая версия текста — пользователи подтверждают предупреждение заново.
        version = str(int(before_notice.version) + 1 if before_notice.version.isdigit() else 1)
        await _put_setting(db, CLOUD_NOTICE_KEY, {"version": version, "text": notice_text}, actor)
        diff["cloud_notice_version"] = {"from": before_notice.version, "to": version}
    if not diff:
        await db.commit()
        return
    revision = (
        await db.execute(
            select(func.coalesce(func.max(ConfigRevision.revision), 0)).where(
                ConfigRevision.entity_type == "setting", ConfigRevision.entity_id == "llm"
            )
        )
    ).scalar_one()
    _add_revision(db, ("setting", "llm", revision + 1), diff, actor)
    record(
        db,
        AuditAction.settings_updated,
        actor_id=actor.user.id,
        target_type="setting",
        target_id="llm",
        ip=actor.ip,
        details={"changes": diff},
    )
    await db.commit()


# ------------------------------------------------------------------ провайдеры


def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
    result = {}
    for name, value in headers.items():
        lowered = name.lower()
        if not _HEADER_NAME.match(name) or lowered in _FORBIDDEN_HEADERS:
            raise AppError(
                "Недопустимое имя дополнительного заголовка", code="invalid_header", status_code=422
            )
        if _SECRETISH_HEADER.search(lowered):
            raise AppError(
                "Секреты нельзя передавать в дополнительных заголовках — используйте поле API-ключа",
                code="secret_in_header",
                status_code=422,
            )
        if len(value) > 500 or any(ch in value for ch in "\r\n\0"):
            raise AppError("Недопустимое значение заголовка", code="invalid_header", status_code=422)
        result[name] = value
    return result


DEFAULT_URLS = {
    ProviderType.openrouter: OPENROUTER_DEFAULT_URL,
    ProviderType.proxyapi: PROXYAPI_DEFAULT_URL,
    ProviderType.ollama: OLLAMA_DEFAULT_URL,
}


def _ensure_key_for_enabled(provider: ProviderConfig) -> None:
    if provider.enabled and provider.type in KEYED_CLOUD_PROVIDERS and provider.secret_ciphertext is None:
        raise AppError(
            f"Для включения {PROVIDER_TITLES[provider.type]} задайте API-ключ",
            code="api_key_missing",
            status_code=422,
        )


def _apply_network_rules(provider: ProviderConfig, requested_is_cloud: bool | None) -> None:
    """Согласует тип, облачность и частную сеть; проверяет адрес (без DNS)."""
    if provider.type in KEYED_CLOUD_PROVIDERS:
        if provider.allow_private_network:
            raise AppError(
                "Частная сеть разрешается только для Ollama",
                code="private_network_not_allowed",
                status_code=422,
            )
        if requested_is_cloud is False:
            raise AppError(
                f"{PROVIDER_TITLES[provider.type]} — облачный провайдер",
                code="cloud_flag_invalid",
                status_code=422,
            )
        provider.is_cloud = True
    elif not provider.allow_private_network:
        # Ollama по публичному адресу: текст уходит во внешнюю сеть — это облачный провайдер.
        if requested_is_cloud is False:
            raise AppError(
                "Локальный провайдер должен находиться в частной сети: включите «разрешить частную сеть»",
                code="local_requires_private_network",
                status_code=422,
            )
        provider.is_cloud = True
    elif requested_is_cloud is not None:
        provider.is_cloud = requested_is_cloud
    try:
        parse_base_url(provider.base_url, network_policy(provider))
    except BlockedDestination as exc:
        raise _blocked_to_app_error(exc) from None


def _set_secret(provider: ProviderConfig, box: SecretBox, api_key: str) -> None:
    key = api_key.strip()
    if len(key) < 8 or any(ch.isspace() for ch in key):
        raise AppError("Некорректный формат ключа", code="invalid_api_key_format", status_code=422)
    provider.secret_ciphertext, provider.secret_key_id = box.encrypt(key)
    provider.secret_last4 = key[-4:]


def _clear_secret(provider: ProviderConfig) -> None:
    provider.secret_ciphertext = None
    provider.secret_key_id = None
    provider.secret_last4 = None


def _provider_state(p: ProviderConfig) -> dict[str, Any]:
    return {
        "name": p.name,
        "type": p.type,
        "base_url": p.base_url,
        "is_cloud": p.is_cloud,
        "enabled": p.enabled,
        "allow_private_network": p.allow_private_network,
        "timeout_s": p.timeout_s,
        "max_concurrency": p.max_concurrency,
        "extra_headers": dict(p.extra_headers or {}),
        # Значение ключа не сохраняется даже в истории — только факт наличия/смены.
        "api_key": (SECRET_MASK + (p.secret_key_id or "")) if p.secret_ciphertext else None,
    }


def _mask_secret_diff(diff: dict[str, Any]) -> dict[str, Any]:
    if "api_key" in diff:
        diff["api_key"] = {
            "from": SECRET_MASK if diff["api_key"]["from"] else None,
            "to": SECRET_MASK if diff["api_key"]["to"] else None,
        }
    return diff


def provider_out(p: ProviderConfig, model_count: int = 0) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        type=p.type,
        base_url=p.base_url,
        is_cloud=p.is_cloud,
        enabled=p.enabled,
        has_secret=p.secret_ciphertext is not None,
        secret_last4=p.secret_last4,
        allow_private_network=p.allow_private_network,
        timeout_s=p.timeout_s,
        max_concurrency=p.max_concurrency,
        extra_headers=dict(p.extra_headers or {}),
        last_check_at=p.last_check_at,
        last_check_status=p.last_check_status,
        last_check_error_safe=p.last_check_error_safe,
        url_warnings=url_warnings(p.base_url),
        model_count=model_count,
        version=p.version,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


async def _model_counts(db: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(ModelConfig.provider_config_id, func.count()).group_by(ModelConfig.provider_config_id)
    )
    return {pid: int(count) for pid, count in rows.all()}


async def list_providers(db: AsyncSession, actor: Actor) -> list[ProviderOut]:
    policies.ensure(policies.can_manage_models(actor.user))
    providers = (await db.execute(select(ProviderConfig).order_by(ProviderConfig.name))).scalars().all()
    counts = await _model_counts(db)
    return [provider_out(p, counts.get(p.id, 0)) for p in providers]


async def get_provider(
    db: AsyncSession, provider_id: uuid.UUID, *, for_update: bool = False
) -> ProviderConfig:
    stmt = select(ProviderConfig).where(ProviderConfig.id == provider_id)
    if for_update:
        stmt = stmt.with_for_update()
    provider = (await db.execute(stmt)).scalar_one_or_none()
    if provider is None:
        raise NotFoundError()
    return provider


async def provider_view(db: AsyncSession, provider: ProviderConfig) -> ProviderOut:
    count = (
        await db.execute(select(func.count()).where(ModelConfig.provider_config_id == provider.id))
    ).scalar_one()
    return provider_out(provider, int(count))


async def _commit_unique(db: AsyncSession, message: str, code: str) -> None:
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError(message, code=code) from None


async def create_provider(
    db: AsyncSession, actor: Actor, box: SecretBox, body: ProviderCreateIn
) -> ProviderConfig:
    policies.ensure(policies.can_manage_providers(actor.user))
    default_url = DEFAULT_URLS[body.type]
    provider = ProviderConfig(
        id=uuid.uuid4(),
        name=body.name,
        type=body.type,
        base_url=(body.base_url or default_url).rstrip("/"),
        is_cloud=body.type in KEYED_CLOUD_PROVIDERS,
        enabled=body.enabled,
        allow_private_network=body.allow_private_network,
        timeout_s=body.timeout_s,
        max_concurrency=body.max_concurrency,
        extra_headers=_validate_headers(body.extra_headers),
    )
    if body.allow_private_network and body.is_cloud is None:
        body = body.model_copy(update={"is_cloud": False})
    _apply_network_rules(provider, body.is_cloud)
    if body.api_key is not None:
        _set_secret(provider, box, body.api_key.get_secret_value())
    _ensure_key_for_enabled(provider)
    db.add(provider)
    await db.flush()
    diff = _mask_secret_diff(_diff({}, _provider_state(provider)))
    _add_revision(db, ("provider", provider.id, provider.version), diff, actor)
    record(
        db,
        AuditAction.provider_created,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"changes": diff},
    )
    await _commit_unique(db, "Провайдер с таким названием уже существует", "provider_name_taken")
    await db.refresh(provider)
    return provider


async def update_provider(
    db: AsyncSession, actor: Actor, box: SecretBox, provider_id: uuid.UUID, body: ProviderUpdateIn
) -> tuple[ProviderConfig, bool]:
    """Возвращает (провайдер, сброшен ли ключ из-за смены адреса)."""
    policies.ensure(policies.can_manage_providers(actor.user))
    provider = await get_provider(db, provider_id, for_update=True)
    _check_version(provider.version, body.version)
    before = _provider_state(provider)
    fields = body.model_fields_set
    if body.name is not None:
        provider.name = body.name
    if body.timeout_s is not None:
        provider.timeout_s = body.timeout_s
    if body.max_concurrency is not None:
        provider.max_concurrency = body.max_concurrency
    if body.extra_headers is not None:
        provider.extra_headers = _validate_headers(body.extra_headers)
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.allow_private_network is not None:
        provider.allow_private_network = body.allow_private_network
    url_changed = False
    if body.base_url is not None and body.base_url.rstrip("/") != provider.base_url:
        provider.base_url = body.base_url.rstrip("/")
        url_changed = True
    requested_cloud = body.is_cloud
    if "allow_private_network" in fields and body.allow_private_network and requested_cloud is None:
        requested_cloud = provider.is_cloud if before["allow_private_network"] else False
    _apply_network_rules(provider, requested_cloud)
    secret_reset = False
    if body.api_key is not None:
        _set_secret(provider, box, body.api_key.get_secret_value())
    elif body.clear_api_key or (url_changed and provider.secret_ciphertext is not None):
        # Смена адреса без нового ключа: старый ключ не должен уйти на новый адрес.
        secret_reset = url_changed and not body.clear_api_key
        _clear_secret(provider)
    _ensure_key_for_enabled(provider)
    after = _provider_state(provider)
    diff = _mask_secret_diff(_diff(before, after))
    if not diff:
        await db.commit()
        await db.refresh(provider)
        return provider, False
    if url_changed or "allow_private_network" in diff or "is_cloud" in diff:
        provider.last_check_at = None
        provider.last_check_status = None
        provider.last_check_error_safe = None
    await db.flush()
    _add_revision(db, ("provider", provider.id, provider.version), diff, actor)
    record(
        db,
        AuditAction.provider_updated,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"changes": diff, "secret_reset": secret_reset},
    )
    await _commit_unique(db, "Провайдер с таким названием уже существует", "provider_name_taken")
    await db.refresh(provider)
    return provider, secret_reset


async def delete_provider(db: AsyncSession, actor: Actor, provider_id: uuid.UUID) -> None:
    policies.ensure(policies.can_manage_providers(actor.user))
    provider = await get_provider(db, provider_id, for_update=True)
    has_models = (
        await db.execute(select(func.count()).where(ModelConfig.provider_config_id == provider.id))
    ).scalar_one()
    if has_models:
        raise ConflictError(
            "У провайдера есть модели — удалите или перенесите их, либо отключите провайдер",
            code="provider_has_models",
        )
    _add_revision(db, ("provider", provider.id, provider.version + 1), {"deleted": True}, actor)
    record(
        db,
        AuditAction.provider_deleted,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"type": provider.type.value},
    )
    await db.delete(provider)
    await db.commit()


def _health_from_error(exc: ProviderError | AppError) -> HealthOut:
    return HealthOut(ok=False, status=exc.code, message=exc.message)


async def check_provider(
    db: AsyncSession, actor: Actor, factory: ProviderFactory, provider_id: uuid.UUID
) -> tuple[ProviderConfig, HealthOut]:
    policies.ensure(policies.can_manage_models(actor.user))
    provider = await get_provider(db, provider_id)
    await db.commit()  # не держим транзакцию во время сетевого запроса
    try:
        health = await factory.build(provider).check_connection()
        result = HealthOut(ok=health.ok, status=health.status, message=health.message, details=health.details)
    except (ProviderError, AppError) as exc:
        result = _health_from_error(exc)
    # Служебные поля статуса — без увеличения version (это не изменение конфигурации).
    await db.execute(
        update(ProviderConfig)
        .where(ProviderConfig.id == provider.id)
        .values(
            last_check_at=func.now(),
            last_check_status=result.status[:32],
            last_check_error_safe=None if result.ok else result.message[:500],
        )
        .execution_options(synchronize_session=False)
    )
    record(
        db,
        AuditAction.provider_checked,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"status": result.status},
    )
    await db.commit()
    await db.refresh(provider)
    return provider, result


async def remote_models(
    db: AsyncSession, actor: Actor, factory: ProviderFactory, provider_id: uuid.UUID, query: str | None
) -> list[RemoteModelOut]:
    policies.ensure(policies.can_manage_models(actor.user))
    provider = await get_provider(db, provider_id)
    existing = set(
        (
            await db.execute(
                select(ModelConfig.external_model_id).where(ModelConfig.provider_config_id == provider.id)
            )
        ).scalars()
    )
    await db.commit()
    try:
        models = await factory.build(provider).list_models()
    except ProviderError as exc:
        raise AppError(exc.message, code=exc.code, status_code=502) from None
    needle = (query or "").strip().lower()
    result = []
    for m in models:
        if needle and needle not in m.external_model_id.lower() and needle not in m.display_name.lower():
            continue
        result.append(
            RemoteModelOut(
                external_model_id=m.external_model_id,
                display_name=m.display_name,
                context_window=m.context_window,
                max_output_tokens=m.max_output_tokens,
                prompt_price_per_mtok=m.prompt_price_per_mtok,
                completion_price_per_mtok=m.completion_price_per_mtok,
                advertises_structured_output=m.advertises_structured_output,
                details=m.details,
                already_added=m.external_model_id in existing,
            )
        )
    result.sort(key=lambda r: r.external_model_id)
    return result


# ------------------------------------------------------------------ модели


def _pricing_from_remote(info: RemoteModelInfo) -> dict[str, Any] | None:
    if info.prompt_price_per_mtok is None or info.completion_price_per_mtok is None:
        return None
    return {
        "prompt_per_mtok": str(info.prompt_price_per_mtok),
        "completion_per_mtok": str(info.completion_price_per_mtok),
        "currency": "USD",
        "source": "provider_api",
        "fetched_at": (info.fetched_at or _now()).isoformat(),
    }


def _pricing_manual(p: PricingIn) -> dict[str, Any]:
    return {
        "prompt_per_mtok": str(p.prompt_per_mtok),
        "completion_per_mtok": str(p.completion_per_mtok),
        "currency": p.currency,
        "source": "admin_manual",
        "fetched_at": _now().isoformat(),
    }


def _remote_metadata(info: RemoteModelInfo) -> dict[str, Any]:
    result: dict[str, Any] = _jsonable(
        {
            "source": "provider_api",
            "fetched_at": info.fetched_at or _now(),
            "context_window": info.context_window,
            "max_output_tokens": info.max_output_tokens,
            "advertises_structured_output": info.advertises_structured_output,
            "details": info.details,
        }
    )
    return result


def pricing_out(value: dict[str, Any] | None) -> PricingOut | None:
    if not value:
        return None
    try:
        return PricingOut.model_validate(value)
    except ValueError:
        return None


def _params_dict(params: GenerationParamsIn) -> dict[str, Any]:
    return params.model_dump(exclude_none=True)


def model_out(m: ModelConfig, p: ProviderConfig) -> ModelOut:
    return ModelOut(
        id=m.id,
        provider_config_id=p.id,
        provider_name=p.name,
        provider_type=p.type,
        provider_enabled=p.enabled,
        is_cloud=p.is_cloud,
        display_name=m.display_name,
        external_model_id=m.external_model_id,
        enabled=m.enabled,
        purpose=list(m.purpose),
        priority=m.priority,
        is_default=m.is_default,
        allowed_roles=list(m.allowed_roles),
        context_window=m.context_window,
        max_output_tokens=m.max_output_tokens,
        generation_params=dict(m.generation_params or {}),
        structured_output=m.structured_output,
        pricing=pricing_out(m.pricing),
        notes=m.notes,
        provider_metadata=m.provider_metadata,
        last_tested_at=m.last_tested_at,
        last_test_status=m.last_test_status,
        last_test_error_safe=m.last_test_error_safe,
        version=m.version,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _model_state(m: ModelConfig) -> dict[str, Any]:
    return {
        "display_name": m.display_name,
        "external_model_id": m.external_model_id,
        "enabled": m.enabled,
        "purpose": sorted(m.purpose),
        "priority": m.priority,
        "is_default": m.is_default,
        "allowed_roles": sorted(m.allowed_roles),
        "context_window": m.context_window,
        "max_output_tokens": m.max_output_tokens,
        "generation_params": dict(m.generation_params or {}),
        "structured_output": m.structured_output,
        "pricing": m.pricing,
        "notes": m.notes,
    }


async def list_models(db: AsyncSession, actor: Actor) -> list[ModelOut]:
    policies.ensure(policies.can_manage_models(actor.user))
    rows = await db.execute(
        select(ModelConfig, ProviderConfig)
        .join(ProviderConfig, ProviderConfig.id == ModelConfig.provider_config_id)
        .order_by(ModelConfig.priority, ModelConfig.display_name)
    )
    return [model_out(m, p) for m, p in rows.all()]


async def get_model(
    db: AsyncSession, model_id: uuid.UUID, *, for_update: bool = False
) -> tuple[ModelConfig, ProviderConfig]:
    stmt = (
        select(ModelConfig, ProviderConfig)
        .join(ProviderConfig, ProviderConfig.id == ModelConfig.provider_config_id)
        .where(ModelConfig.id == model_id)
    )
    if for_update:
        stmt = stmt.with_for_update(of=ModelConfig)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise NotFoundError()
    return row[0], row[1]


async def _clear_other_defaults(db: AsyncSession, model_id: uuid.UUID) -> None:
    await db.execute(
        update(ModelConfig)
        .where(ModelConfig.is_default.is_(True), ModelConfig.id != model_id)
        .values(is_default=False, version=ModelConfig.version + 1)
        .execution_options(synchronize_session=False)
    )


def _check_default(m: ModelConfig) -> None:
    if m.is_default and (not m.enabled or ModelPurpose.analysis.value not in m.purpose):
        raise AppError(
            "Моделью по умолчанию может быть только включённая модель с назначением «анализ»",
            code="invalid_default_model",
            status_code=422,
        )


async def create_model(db: AsyncSession, actor: Actor, body: ModelCreateIn) -> ModelConfig:
    policies.ensure(policies.can_manage_models(actor.user))
    provider = await get_provider(db, body.provider_config_id)
    model = ModelConfig(
        id=uuid.uuid4(),
        provider_config_id=provider.id,
        display_name=body.display_name,
        external_model_id=body.external_model_id,
        enabled=body.enabled,
        purpose=sorted({p.value for p in body.purpose}),
        priority=body.priority,
        is_default=body.is_default,
        allowed_roles=sorted({r.value for r in body.allowed_roles}),
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        generation_params=_params_dict(body.generation_params),
        structured_output=StructuredOutputSupport(body.structured_output),
        pricing=_pricing_manual(body.pricing) if body.pricing else None,
        notes=body.notes,
    )
    _check_default(model)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('model_default'))"))
    if model.is_default:
        await _clear_other_defaults(db, model.id)
    db.add(model)
    await db.flush()
    diff = _diff({}, _model_state(model))
    _add_revision(db, ("model", model.id, model.version), diff, actor)
    record(
        db,
        AuditAction.model_created,
        actor_id=actor.user.id,
        target_type="model",
        target_id=model.id,
        ip=actor.ip,
        details={"provider": str(provider.id), "external_model_id": model.external_model_id},
    )
    await _commit_unique(db, "Такая модель у этого провайдера уже есть", "model_already_exists")
    await db.refresh(model)
    return model


async def update_model(  # noqa: PLR0912 - по ветке на поле
    db: AsyncSession, actor: Actor, model_id: uuid.UUID, body: ModelUpdateIn
) -> ModelConfig:
    policies.ensure(policies.can_manage_models(actor.user))
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('model_default'))"))
    model, _provider = await get_model(db, model_id, for_update=True)
    _check_version(model.version, body.version)
    before = _model_state(model)
    fields = body.model_fields_set
    if body.display_name is not None:
        model.display_name = body.display_name
    if body.enabled is not None:
        model.enabled = body.enabled
        if not body.enabled:
            model.is_default = False
    if body.purpose is not None:
        model.purpose = sorted({p.value for p in body.purpose})
    if body.priority is not None:
        model.priority = body.priority
    if body.allowed_roles is not None:
        model.allowed_roles = sorted({r.value for r in body.allowed_roles})
    if "context_window" in fields:
        model.context_window = body.context_window
    if "max_output_tokens" in fields:
        model.max_output_tokens = body.max_output_tokens
    if body.generation_params is not None:
        model.generation_params = _params_dict(body.generation_params)
    if body.structured_output is not None:
        model.structured_output = StructuredOutputSupport(body.structured_output)
    if "pricing" in fields:
        model.pricing = _pricing_manual(body.pricing) if body.pricing else None
    if "notes" in fields:
        model.notes = body.notes
    if body.is_default is not None:
        model.is_default = body.is_default
    _check_default(model)
    after = _model_state(model)
    diff = _diff(before, after)
    if not diff:
        await db.commit()
        await db.refresh(model)
        return model
    if model.is_default:
        await _clear_other_defaults(db, model.id)
    await db.flush()
    _add_revision(db, ("model", model.id, model.version), diff, actor)
    record(
        db,
        AuditAction.model_updated,
        actor_id=actor.user.id,
        target_type="model",
        target_id=model.id,
        ip=actor.ip,
        details={"changes": diff},
    )
    await db.commit()
    await db.refresh(model)
    return model


async def delete_model(db: AsyncSession, actor: Actor, model_id: uuid.UUID) -> None:
    policies.ensure(policies.can_manage_models(actor.user))
    model, _provider = await get_model(db, model_id, for_update=True)
    used = (await db.execute(select(func.count()).where(Analysis.model_config_id == model.id))).scalar_one()
    if used:
        raise ConflictError(
            "Модель использовалась в анализах — её можно только отключить", code="model_in_use"
        )
    _add_revision(db, ("model", model.id, model.version + 1), {"deleted": True}, actor)
    record(
        db,
        AuditAction.model_deleted,
        actor_id=actor.user.id,
        target_type="model",
        target_id=model.id,
        ip=actor.ip,
        details={"external_model_id": model.external_model_id},
    )
    await db.delete(model)
    await db.commit()


async def import_models(
    db: AsyncSession, actor: Actor, factory: ProviderFactory, provider_id: uuid.UUID, external_ids: list[str]
) -> list[ModelConfig]:
    """Добавляет выбранные модели провайдера (выключенными) со сведениями из его API."""
    policies.ensure(policies.can_manage_models(actor.user))
    provider = await get_provider(db, provider_id)
    await db.commit()
    try:
        remote = {m.external_model_id: m for m in await factory.build(provider).list_models()}
    except ProviderError as exc:
        raise AppError(exc.message, code=exc.code, status_code=502) from None
    wanted = list(dict.fromkeys(external_ids))
    unknown = [mid for mid in wanted if mid not in remote]
    if unknown:
        raise AppError(
            "Провайдер не сообщает о части выбранных моделей",
            code="remote_model_not_found",
            status_code=422,
            details={"external_model_ids": unknown[:20]},
        )
    existing = set(
        (
            await db.execute(
                select(ModelConfig.external_model_id).where(ModelConfig.provider_config_id == provider.id)
            )
        ).scalars()
    )
    created = []
    for mid in wanted:
        if mid in existing:
            continue
        info = remote[mid]
        model = ModelConfig(
            id=uuid.uuid4(),
            provider_config_id=provider.id,
            display_name=info.display_name,
            external_model_id=mid,
            enabled=False,
            purpose=[ModelPurpose.analysis.value, ModelPurpose.verifier.value],
            priority=100,
            is_default=False,
            allowed_roles=sorted([UserRole.superadmin.value, UserRole.admin.value, UserRole.analyst.value]),
            context_window=info.context_window,
            max_output_tokens=info.max_output_tokens,
            generation_params={},
            structured_output=StructuredOutputSupport.unknown,
            pricing=_pricing_from_remote(info),
            provider_metadata=_remote_metadata(info),
        )
        db.add(model)
        created.append(model)
    await db.flush()
    for model in created:
        _add_revision(db, ("model", model.id, model.version), _diff({}, _model_state(model)), actor)
    record(
        db,
        AuditAction.models_imported,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"count": len(created), "external_model_ids": [m.external_model_id for m in created][:50]},
    )
    await _commit_unique(db, "Модели изменились во время импорта, повторите", "model_already_exists")
    for model in created:
        await db.refresh(model)
    return created


# ------------------------------------------------------------------ проверка модели

_TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}
_TEST_MESSAGES = [
    ChatMessage("system", "Ты сервис проверки связи. Отвечай только JSON."),
    ChatMessage("user", 'Верни JSON-объект {"status": "ok"}.'),
]
# Лимит ответа тестовой генерации: модели с рассуждением (deepseek-r1 и т.п.) расходуют его и на
# рассуждение — 200 токенов им не хватало даже на ответ {"status": "ok"} (проверено на Ollama).
PROBE_MAX_OUTPUT_TOKENS = 2048
TRUNCATED_HINT = (
    "Ответ модели обрезан по лимиту токенов. Увеличьте «Максимум токенов ответа»: у моделей "
    "с рассуждением (reasoning/thinking) лимит расходуется и на рассуждение"
)
# Ошибки, при которых нет смысла пробовать другой режим structured output.
_FATAL_TEST_CODES = frozenset(
    {
        "provider_auth_failed",
        "provider_quota_exceeded",
        "provider_timeout",
        "provider_rate_limited",
        "provider_unavailable",
        "connection_failed",
        "dns_failed",
        "api_key_missing",
        "destination_blocked",
    }
)


async def test_model(
    db: AsyncSession, actor: Actor, factory: ProviderFactory, model_id: uuid.UUID, *, generate: bool
) -> tuple[ModelConfig, ProviderConfig, bool, list[TestStep]]:
    """Проверка модели: наличие у провайдера (+ сведения API) и, по запросу, тестовая генерация.

    `structured_output=json_schema` выставляется только после успешного вызова со схемой.
    Тестовый запрос не содержит данных договоров.
    """
    policies.ensure(policies.can_manage_models(actor.user))
    model, provider = await get_model(db, model_id)
    await db.commit()
    steps: list[TestStep] = []
    info: RemoteModelInfo | None = None
    confirmed: StructuredOutputSupport | None = None
    try:
        llm = factory.build(provider)
        info = await llm.describe_model(model.external_model_id)
        steps.append(TestStep(name="describe", ok=True, code="ok", message="Модель найдена у провайдера"))
        if generate:
            confirmed = await _probe_generation(llm, model, steps)
    except (ProviderError, AppError) as exc:
        steps.append(
            TestStep(
                name="describe" if info is None else "generate", ok=False, code=exc.code, message=exc.message
            )
        )
    ok = all(s.ok for s in steps)
    failed = next((s for s in steps if not s.ok), None)

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('model_default'))"))
    model, provider = await get_model(db, model_id, for_update=True)
    before = _model_state(model)
    if info is not None:
        model.provider_metadata = _remote_metadata(info)
        if model.context_window is None and info.context_window:
            model.context_window = info.context_window
        if model.max_output_tokens is None and info.max_output_tokens:
            model.max_output_tokens = info.max_output_tokens
        if model.pricing is None or model.pricing.get("source") == "provider_api":
            model.pricing = _pricing_from_remote(info)
    if confirmed is not None:
        model.structured_output = confirmed
    model.last_tested_at = _now()
    model.last_test_status = "ok" if ok else (failed.code[:32] if failed else "error")
    model.last_test_error_safe = None if ok or failed is None else failed.message[:500]
    await db.flush()
    diff = _diff(before, _model_state(model))
    if diff:
        _add_revision(db, ("model", model.id, model.version), diff, actor)
    record(
        db,
        AuditAction.model_tested,
        actor_id=actor.user.id,
        target_type="model",
        target_id=model.id,
        ip=actor.ip,
        details={"status": model.last_test_status, "generate": generate, "changes": diff},
    )
    await db.commit()
    await db.refresh(model)
    return model, provider, ok, steps


async def _probe_generation(
    llm: Any, model: ModelConfig, steps: list[TestStep]
) -> StructuredOutputSupport | None:
    limit = min(model.max_output_tokens or PROBE_MAX_OUTPUT_TOKENS, PROBE_MAX_OUTPUT_TOKENS)
    # Режим рассуждений — как в анализе: проверка покажет, принимает ли его модель.
    reasoning = GenerationParams.from_mapping(dict(model.generation_params or {})).reasoning
    params = GenerationParams(temperature=0, max_output_tokens=limit, reasoning=reasoning)
    last_error: ProviderError | None = None
    for mode in (
        StructuredOutputSupport.json_schema,
        StructuredOutputSupport.json_mode,
        StructuredOutputSupport.none,
    ):
        request = StructuredRequest(
            model=model.external_model_id,
            messages=_TEST_MESSAGES,
            json_schema=_TEST_SCHEMA,
            schema_name="connectivity_check",
            params=params,
            structured_output=mode,
        )
        try:
            result = await llm.generate_structured(request)
        except ProviderError as exc:
            if exc.code in _FATAL_TEST_CODES:
                raise
            if exc.code == "output_truncated":
                # Другой режим не поможет: модели не хватает лимита ответа.
                raise InvalidModelOutput(TRUNCATED_HINT, code="output_truncated") from None
            last_error = exc
            continue
        if result.parsed_json == {"status": "ok"}:
            steps.append(
                TestStep(
                    name="generate",
                    ok=True,
                    code=mode.value,
                    message=_MODE_MESSAGES[mode],
                )
            )
            return mode
    steps.append(
        TestStep(
            name="generate",
            ok=False,
            code=last_error.code if last_error else "invalid_model_output",
            message="Модель не вернула ответ в требуемом формате ни в одном режиме",
        )
    )
    return None


_MODE_MESSAGES = {
    StructuredOutputSupport.json_schema: "Генерация по JSON Schema подтверждена тестовым запросом",
    StructuredOutputSupport.json_mode: "Схема не поддерживается; подтверждён JSON-режим",
    StructuredOutputSupport.none: "Ни схема, ни JSON-режим не подтверждены; ответ получен по инструкции",
}


# ------------------------------------------------------------------ доступность для пользователя


async def _overrides(db: AsyncSession, user: User) -> dict[uuid.UUID, bool]:
    rows = await db.execute(
        select(UserModelPermission.model_config_id, UserModelPermission.allowed).where(
            UserModelPermission.user_id == user.id
        )
    )
    return {mid: allowed for mid, allowed in rows.all()}


def _role_or_override(user: User, model: ModelConfig, override: bool | None) -> bool:
    if override is not None:
        return override
    return user.role.value in model.allowed_roles


async def _consented_providers(db: AsyncSession, user: User, notice_version: str) -> set[uuid.UUID]:
    rows = await db.execute(
        select(ConsentRecord.provider_config_id).where(
            ConsentRecord.user_id == user.id, ConsentRecord.notice_version == notice_version
        )
    )
    return set(rows.scalars())


async def available_models(
    db: AsyncSession, user: User, purpose: ModelPurpose = ModelPurpose.analysis
) -> tuple[list[AvailableModelOut], bool, CloudNoticeOut]:
    """Модели, которые пользователь может выбрать сейчас (то же правило, что в `resolve_model_for_use`)."""
    cloud_enabled = await cloud_models_enabled(db)
    notice = await cloud_notice(db)
    if user.role not in policies.CONTRIBUTOR_ROLES:
        return [], cloud_enabled, notice
    overrides = await _overrides(db, user)
    consented = await _consented_providers(db, user, notice.version)
    rows = await db.execute(
        select(ModelConfig, ProviderConfig)
        .join(ProviderConfig, ProviderConfig.id == ModelConfig.provider_config_id)
        .where(
            ModelConfig.enabled.is_(True),
            ProviderConfig.enabled.is_(True),
            ModelConfig.purpose.contains([purpose.value]),
        )
        .order_by(ModelConfig.is_default.desc(), ModelConfig.priority, ModelConfig.display_name)
    )
    items = []
    for model, provider in rows.all():
        if provider.is_cloud and not cloud_enabled:
            continue
        if not _role_or_override(user, model, overrides.get(model.id)):
            continue
        items.append(
            AvailableModelOut(
                id=model.id,
                display_name=model.display_name,
                provider_name=provider.name,
                provider_type=provider.type,
                is_cloud=provider.is_cloud,
                is_default=model.is_default,
                purpose=list(model.purpose),
                context_window=model.context_window,
                pricing=pricing_out(model.pricing),
                consent_required=provider.is_cloud and provider.id not in consented,
            )
        )
    return items, cloud_enabled, notice


class ModelNotAvailable(AppError):
    status_code = 404
    code = "model_not_found"
    message = "Модель не найдена или недоступна"


async def resolve_model_for_use(
    db: AsyncSession,
    user: User,
    model_id: uuid.UUID,
    *,
    purpose: ModelPurpose = ModelPurpose.analysis,
    cloud_consent: bool,
) -> tuple[ModelConfig, ProviderConfig]:
    """Серверная проверка выбора модели перед анализом (этап 06 вызывает её при запуске).

    Нет прав (роль/персональный запрет) или модели нет → 404; отключена → 409 model_disabled;
    облако выключено → 409 cloud_models_disabled; нет подтверждения → 409 cloud_consent_required.
    """
    if user.role not in policies.CONTRIBUTOR_ROLES:
        raise PermissionDenied()
    row = (
        await db.execute(
            select(ModelConfig, ProviderConfig)
            .join(ProviderConfig, ProviderConfig.id == ModelConfig.provider_config_id)
            .where(ModelConfig.id == model_id)
        )
    ).one_or_none()
    if row is None:
        raise ModelNotAvailable()
    model, provider = row
    override = (await _overrides(db, user)).get(model.id)
    if not _role_or_override(user, model, override):
        raise ModelNotAvailable()
    if not model.enabled or not provider.enabled:
        raise ConflictError("Модель отключена администратором", code="model_disabled")
    if purpose.value not in model.purpose:
        raise AppError(
            "Модель не предназначена для этой операции", code="model_purpose_mismatch", status_code=422
        )
    if provider.is_cloud:
        if not await cloud_models_enabled(db):
            raise ConflictError("Облачные модели отключены администратором", code="cloud_models_disabled")
        if not cloud_consent:
            raise ConflictError(
                "Подтвердите передачу текста договора внешнему провайдеру", code="cloud_consent_required"
            )
    return model, provider


def config_snapshot(model: ModelConfig, provider: ProviderConfig) -> dict[str, Any]:
    """Snapshot конфигурации для `Analysis.config_snapshot` — без секретов и без полного URL."""
    try:
        host = parse_base_url(provider.base_url, network_policy(provider)).host
    except BlockedDestination:
        host = None
    result: dict[str, Any] = _jsonable(
        {
            "captured_at": _now(),
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "type": provider.type,
                "host": host,
                "is_cloud": provider.is_cloud,
                "version": provider.version,
            },
            "model": {
                "id": model.id,
                "display_name": model.display_name,
                "external_model_id": model.external_model_id,
                "context_window": model.context_window,
                "max_output_tokens": model.max_output_tokens,
                "generation_params": dict(model.generation_params or {}),
                "structured_output": model.structured_output,
                "pricing": model.pricing,
                "version": model.version,
            },
        }
    )
    return result


async def record_cloud_consent(
    db: AsyncSession, actor: Actor, model_id: uuid.UUID, notice_version: str
) -> ConsentRecord:
    model, provider = await get_model(db, model_id)
    items, _cloud_enabled, notice = await available_models(db, actor.user)
    if not any(i.id == model.id for i in items):
        raise ModelNotAvailable()
    if not provider.is_cloud:
        raise AppError(
            "Модель не облачная — подтверждение не требуется", code="consent_not_required", status_code=422
        )
    if notice_version != notice.version:
        raise ConflictError(
            "Текст предупреждения изменился — ознакомьтесь с новой версией", code="notice_outdated"
        )
    await db.execute(
        insert(ConsentRecord)
        .values(
            id=uuid.uuid4(),
            user_id=actor.user.id,
            provider_config_id=provider.id,
            notice_version=notice.version,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "provider_config_id", "notice_version"])
    )
    record(
        db,
        AuditAction.cloud_consent_recorded,
        actor_id=actor.user.id,
        target_type="provider",
        target_id=provider.id,
        ip=actor.ip,
        details={"notice_version": notice.version, "model": str(model.id)},
    )
    await db.commit()
    consent = (
        await db.execute(
            select(ConsentRecord).where(
                ConsentRecord.user_id == actor.user.id,
                ConsentRecord.provider_config_id == provider.id,
                ConsentRecord.notice_version == notice.version,
            )
        )
    ).scalar_one()
    return consent


async def list_revisions(
    db: AsyncSession, actor: Actor, entity_type: str | None, entity_id: str | None, limit: int
) -> list[ConfigRevision]:
    policies.ensure(policies.can_manage_models(actor.user))
    stmt = select(ConfigRevision).order_by(ConfigRevision.created_at.desc(), ConfigRevision.revision.desc())
    if entity_type:
        stmt = stmt.where(ConfigRevision.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(ConfigRevision.entity_id == entity_id)
    return list((await db.execute(stmt.limit(limit))).scalars())


async def rotate_provider_secrets(db: AsyncSession, box: SecretBox) -> tuple[int, int]:
    """Перешифровывает ключи провайдеров основным ключом (`python -m app.cli rotate-secrets`).

    Возвращает (перешифровано, не расшифровалось). Значения ключей не выводятся.
    """
    rows = (
        await db.execute(
            select(ProviderConfig.id, ProviderConfig.secret_ciphertext, ProviderConfig.secret_key_id)
            .where(ProviderConfig.secret_ciphertext.is_not(None))
            .with_for_update()
        )
    ).all()
    rotated = failed = 0
    for provider_id, ciphertext, key_id in rows:
        if ciphertext is None or key_id == box.primary_key_id:
            continue
        try:
            new_ciphertext, new_key_id = box.rotate(ciphertext)
        except AppError:
            failed += 1
            continue
        await db.execute(
            update(ProviderConfig)
            .where(ProviderConfig.id == provider_id)
            .values(secret_ciphertext=new_ciphertext, secret_key_id=new_key_id)
            .execution_options(synchronize_session=False)
        )
        rotated += 1
    await db.commit()
    return rotated, failed
