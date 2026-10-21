"""Маскировка перед облачным анализом (этап 10, docs/REDACTION_PLAN.md §5–6).

- Настройки (`system_settings.redaction`) меняет только Superadmin; каждое изменение — ревизия в
  `config_revisions` (без значений словаря и шаблонов — только их число) и аудит. Включение
  `allow_unmasked` — только с явным подтверждением.
- Предпросмотр создаёт пользователь с правом анализа документа; обнаружение выполняет задача
  `redaction_scan` в worker'е. Найденные значения хранятся зашифрованными и видны только автору.
- При запуске облачного анализа подтверждённый предпросмотр расходуется: строится таблица
  соответствий (зашифрована), сами найденные данные предпросмотра удаляются.
- Значения никогда не пишутся в логи, аудит и ревизии — только счётчики.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, exists, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AppError, ConflictError, PermissionDenied
from app.jobs import queue
from app.logging import get_logger
from app.models.documents import DocumentFragment, DocumentVersion
from app.models.enums import ExtractionStatus, RedactionMode, RedactionPreviewStatus
from app.models.redaction import AnalysisRedaction, RedactionPreview
from app.models.system import ConfigRevision, SystemSetting
from app.models.users import User
from app.redaction import REDACTION_VERSION
from app.redaction.categories import BY_CODE, CATEGORIES, CODES, SHORT_TITLES
from app.redaction.mask import build_mapping, mapping_counts
from app.redaction.settings import InvalidPattern, RedactionSettings, check_pattern
from app.schemas.redaction import (
    AddMarkOp,
    CategoryOut,
    EditOp,
    EntityOut,
    OccurrenceOut,
    PreviewConfirmIn,
    PreviewOut,
    SetCategoryOp,
    SetEntityOp,
    SetOccurrenceOp,
)
from app.security import policies
from app.security.policies import Actor
from app.security.scoped import DocumentAction, get_version_for
from app.security.secretbox import SecretBox, SecretsNotConfigured
from app.services.audit import AuditAction, record

log = get_logger(__name__)

SETTINGS_KEY = "redaction"
SCAN_JOB = "redaction_scan"
SCAN_MAX_ATTEMPTS = 2
# Версия текста предупреждения об отправке без маскировки (фронтенд показывает его и возвращает версию).
UNMASKED_NOTICE_VERSION = "unmasked-1"
MAX_ACTIVE_PREVIEWS_PER_USER = 5
MAX_MARK_CHARS = 500

_ERROR_MESSAGES = {
    "redaction_scan_timeout": "Поиск данных не уложился в лимит времени. Попробуйте ещё раз.",
    "resource_limit_exceeded": "Поиск данных превысил лимит памяти.",
    "redaction_scan_crashed": "Поиск данных завершился с ошибкой. Попробуйте ещё раз.",
    "version_has_no_text": "В версии документа нет текста.",
    "internal_error": "Внутренняя ошибка при поиске данных.",
}


def _now() -> datetime:
    return datetime.now(UTC)


def categories_out() -> list[CategoryOut]:
    return [
        CategoryOut(
            code=c.code,
            title=c.title,
            short=SHORT_TITLES.get(c.code, c.title),
            group=c.group.value,
            method=c.method,
            reliability=c.reliability.value,
            label=c.label,
            default_enabled=c.default_enabled,
            warning=c.warning,
            supported=c.supported,
        )
        for c in CATEGORIES
    ]


# ================================================================== настройки


@dataclass(frozen=True)
class SettingsState:
    settings: RedactionSettings
    revision: int
    updated_at: datetime | None
    updated_by_id: uuid.UUID | None


async def _revision(db: AsyncSession) -> int:
    value = (
        await db.execute(
            select(func.coalesce(func.max(ConfigRevision.revision), 0)).where(
                ConfigRevision.entity_type == "setting", ConfigRevision.entity_id == SETTINGS_KEY
            )
        )
    ).scalar_one()
    return int(value)


async def get_state(db: AsyncSession) -> SettingsState:
    row = (
        await db.execute(
            select(SystemSetting.value, SystemSetting.updated_at, SystemSetting.updated_by_id).where(
                SystemSetting.key == SETTINGS_KEY
            )
        )
    ).first()
    settings = RedactionSettings()
    if row is not None:
        try:
            settings = RedactionSettings.model_validate(row[0])
        except ValidationError:
            # Повреждённая запись: безопасные значения по умолчанию (маскировка обязательна).
            log.warning("redaction_settings_invalid")
    return SettingsState(settings, await _revision(db), row[1] if row else None, row[2] if row else None)


def validate_patterns(settings: RedactionSettings) -> None:
    for index, item in enumerate(settings.custom_patterns):
        try:
            check_pattern(item.pattern)
        except InvalidPattern as exc:
            raise AppError(
                f"Шаблон № {index + 1}: {exc}",
                code="redaction_invalid_pattern",
                status_code=422,
                details={"index": index, "reason": str(exc)},
            ) from None


def _diff(before: RedactionSettings, after: RedactionSettings) -> dict[str, Any]:
    """Изменения без значений словаря, шаблонов и исключений (только их число)."""
    diff: dict[str, Any] = {}
    changed = {
        code: {"from": before.categories.get(code), "to": on}
        for code, on in after.categories.items()
        if before.categories.get(code) != on
    }
    if changed:
        diff["categories"] = changed
    if before.allow_unmasked != after.allow_unmasked:
        diff["allow_unmasked"] = {"from": before.allow_unmasked, "to": after.allow_unmasked}
    for name in ("custom_dictionary", "custom_patterns", "allow_list"):
        a, b = getattr(before, name), getattr(after, name)
        if a != b:
            diff[name] = {"from": len(a), "to": len(b)}
    if before.preview_ttl_hours != after.preview_ttl_hours:
        diff["preview_ttl_hours"] = {"from": before.preview_ttl_hours, "to": after.preview_ttl_hours}
    return diff


async def update_settings(
    db: AsyncSession, actor: User, ip: str | None, new: RedactionSettings, *, confirm_allow_unmasked: bool
) -> SettingsState:
    policies.ensure(policies.can_manage_redaction(actor))
    validate_patterns(new)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('redaction_settings'))"))
    before = (await get_state(db)).settings
    diff = _diff(before, new)
    if not diff:
        await db.commit()
        return await get_state(db)
    enabling_unmasked = new.allow_unmasked and not before.allow_unmasked
    if enabling_unmasked and not confirm_allow_unmasked:
        await db.commit()
        raise ConflictError(
            "Подтвердите разрешение отправлять текст в облако без маскировки",
            code="redaction_unmasked_confirmation_required",
        )
    value = new.model_dump(mode="json")
    await db.execute(
        pg_insert(SystemSetting)
        .values(key=SETTINGS_KEY, value=value, updated_by_id=actor.id)
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": value, "updated_by_id": actor.id, "updated_at": func.now()},
        )
    )
    await db.execute(
        insert(ConfigRevision).values(
            entity_type="setting",
            entity_id=SETTINGS_KEY,
            revision=await _revision(db) + 1,
            diff=diff,
            changed_by_id=actor.id,
        )
    )
    record(
        db,
        AuditAction.redaction_settings_updated,
        actor_id=actor.id,
        target_type="setting",
        target_id=SETTINGS_KEY,
        ip=ip,
        details={"changes": diff},
    )
    if "allow_unmasked" in diff:
        record(
            db,
            AuditAction.redaction_unmasked_allowed
            if new.allow_unmasked
            else AuditAction.redaction_unmasked_disallowed,
            actor_id=actor.id,
            target_type="setting",
            target_id=SETTINGS_KEY,
            ip=ip,
            details={"confirmed": bool(confirm_allow_unmasked)} if new.allow_unmasked else {},
        )
    await db.commit()
    log.info("redaction_settings_updated", fields=sorted(diff))
    return await get_state(db)


# ================================================================== шифрование


def _encrypt(box: SecretBox, value: dict[str, Any]) -> tuple[bytes, str]:
    return box.encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _decrypt(box: SecretBox, ciphertext: bytes) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(box.decrypt(ciphertext))
    return data


def _require_box(box: SecretBox) -> None:
    if not box.configured:
        raise SecretsNotConfigured()


# ================================================================== предпросмотры


async def create_preview(
    db: AsyncSession,
    actor: User,
    box: SecretBox,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> RedactionPreview:
    if not policies.can_upload_documents(actor):
        raise PermissionDenied()
    _require_box(box)
    document, version = await get_version_for(db, actor, document_id, version_id, DocumentAction.analyze)
    if version.extraction_status is not ExtractionStatus.ready:
        raise ConflictError(
            "Текст версии ещё не извлечён или извлечение не удалось", code="version_not_ready"
        )
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"redaction-preview:{actor.id}"))))
    active = (
        await db.execute(
            select(func.count())
            .select_from(RedactionPreview)
            .where(
                RedactionPreview.created_by_id == actor.id,
                RedactionPreview.status == RedactionPreviewStatus.scanning,
            )
        )
    ).scalar_one()
    if active >= MAX_ACTIVE_PREVIEWS_PER_USER:
        raise AppError(
            "Слишком много предпросмотров в обработке. Дождитесь завершения.",
            code="too_many_redaction_previews",
            status_code=429,
        )
    state = await get_state(db)
    preview = RedactionPreview(
        id=uuid.uuid4(),
        document_id=document.id,
        version_id=version.id,
        created_by_id=actor.id,
        settings_revision=state.revision,
        redaction_version=REDACTION_VERSION,
        status=RedactionPreviewStatus.scanning,
        counts={},
        expires_at=_now() + timedelta(hours=state.settings.preview_ttl_hours),
    )
    db.add(preview)
    await db.flush()
    await queue.enqueue(
        db,
        SCAN_JOB,
        {"preview_id": str(preview.id)},
        priority=50,
        max_attempts=SCAN_MAX_ATTEMPTS,
        dedup_key=f"{SCAN_JOB}:{preview.id}",
    )
    await db.commit()
    await db.refresh(preview)
    return preview


def _preview_counts(entities: list[dict[str, Any]], *, ner: bool | None) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    occurrences = manual = unmarked = review = 0
    for e in entities:
        masked_occ = [o for o in e["occurrences"] if o.get("masked", True)] if e.get("masked", True) else []
        if e.get("source") == "manual":
            manual += 1
        if not e.get("masked", True):
            unmarked += 1
        else:
            unmarked += sum(1 for o in e["occurrences"] if not o.get("masked", True))
        if masked_occ:
            by_category[e["category"]] = by_category.get(e["category"], 0) + 1
            occurrences += len(masked_occ)
        if e.get("confidence") == "low":
            review += 1
    return {
        "categories": dict(sorted(by_category.items())),
        "values": sum(by_category.values()),
        "occurrences": occurrences,
        "manual": manual,
        "unmarked": unmarked,
        "review": review,
        "ner": ner,
    }


async def run_scan_job(
    session_factory: async_sessionmaker[AsyncSession],
    box: SecretBox,
    preview_id: uuid.UUID,
    *,
    timeout_seconds: int,
    memory_mb: int,
    last_attempt: bool,
) -> None:
    """Задача `redaction_scan`: обнаружение в дочернем процессе и сохранение (зашифрованно)."""
    from app.jobs.registry import JobError  # noqa: PLC0415 - избегаем циклического импорта
    from app.redaction.sandbox import ScanFailed, ScanLimits, run_scan  # noqa: PLC0415

    async with session_factory() as db:
        preview = await db.get(RedactionPreview, preview_id)
        if preview is None or preview.status is not RedactionPreviewStatus.scanning:
            await db.commit()
            return
        rows = (
            await db.execute(
                select(DocumentFragment.fragment_key, DocumentFragment.text)
                .where(DocumentFragment.version_id == preview.version_id)
                .order_by(DocumentFragment.seq)
            )
        ).all()
        state = await get_state(db)
        await db.commit()
    fragments = [(str(k), str(t)) for k, t in rows]

    async def fail(code: str) -> None:
        async with session_factory() as db:
            await db.execute(
                update(RedactionPreview)
                .where(
                    RedactionPreview.id == preview_id,
                    RedactionPreview.status == RedactionPreviewStatus.scanning,
                )
                .values(status=RedactionPreviewStatus.failed, error_code=code)
            )
            await db.commit()
        log.warning("redaction_scan_failed", code=code)

    if not fragments:
        await fail("version_has_no_text")
        return
    try:
        result = await asyncio.to_thread(
            run_scan, fragments, state.settings.scan_config(), ScanLimits(timeout_seconds, memory_mb)
        )
    except ScanFailed as exc:
        if last_attempt or exc.code == "resource_limit_exceeded":
            await fail(exc.code)
            return
        raise JobError(exc.code, retryable=True) from None
    entities: list[dict[str, Any]] = result["entities"]
    counts = _preview_counts(entities, ner=result.get("ner"))
    ciphertext, key_id = _encrypt(box, {"entities": entities, "ner": result.get("ner"), "next_manual": 1})
    async with session_factory() as db:
        await db.execute(
            update(RedactionPreview)
            .where(
                RedactionPreview.id == preview_id, RedactionPreview.status == RedactionPreviewStatus.scanning
            )
            .values(
                status=RedactionPreviewStatus.ready,
                entities_encrypted=ciphertext,
                secret_key_id=key_id,
                counts=counts,
                settings_revision=state.revision,
            )
        )
        await db.commit()
    log.info("redaction_scan_done", values=counts["values"], occurrences=counts["occurrences"])


async def _fragment_seq(db: AsyncSession, version_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await db.execute(
            select(DocumentFragment.fragment_key, DocumentFragment.seq).where(
                DocumentFragment.version_id == version_id
            )
        )
    ).all()
    return {str(k): int(v) for k, v in rows}


async def _is_used(db: AsyncSession, preview_id: uuid.UUID) -> bool:
    stmt = select(exists().where(AnalysisRedaction.preview_id == preview_id))
    return bool((await db.execute(stmt)).scalar_one())


def _stale_reason(preview: RedactionPreview, state: SettingsState) -> str | None:
    if preview.expires_at <= _now():
        return "expired"
    if preview.settings_revision != state.revision:
        return "settings_changed"
    return None


async def preview_view(db: AsyncSession, box: SecretBox, preview: RedactionPreview) -> PreviewOut:
    state = await get_state(db)
    used = await _is_used(db, preview.id)
    await db.commit()
    data: dict[str, Any] = {"entities": [], "ner": None}
    if preview.entities_encrypted is not None and preview.status in (
        RedactionPreviewStatus.ready,
        RedactionPreviewStatus.confirmed,
    ):
        data = _decrypt(box, preview.entities_encrypted)
    reason = _stale_reason(preview, state)
    labels: dict[str, str] = {}
    if data["entities"]:
        seq = await _fragment_seq(db, preview.version_id)
        await db.commit()
        mapping = build_mapping(data["entities"], seq)
        labels = {str(info["entity"]): label for label, info in mapping["labels"].items()}
    return PreviewOut(
        id=preview.id,
        document_id=preview.document_id,
        version_id=preview.version_id,
        status=preview.status.value,
        error_code=preview.error_code,
        error_message=_ERROR_MESSAGES.get(preview.error_code or "") if preview.error_code else None,
        created_at=preview.created_at,
        expires_at=preview.expires_at,
        confirmed_at=preview.confirmed_at,
        mode=preview.mode.value if preview.mode else None,
        stale=reason is not None and not used,
        stale_reason=reason if not used else None,
        used=used,
        counts=dict(preview.counts or {}),
        ner_available=data.get("ner"),
        allow_unmasked=state.settings.allow_unmasked,
        entities=[
            EntityOut(
                id=e["id"],
                category=e["category"],
                canonical=e["canonical"],
                confidence=e["confidence"],
                source=e["source"],
                masked=bool(e.get("masked", True)),
                label=labels.get(e["id"]),
                occurrences=[
                    OccurrenceOut(**{**o, "masked": bool(o.get("masked", True))}) for o in e["occurrences"]
                ],
            )
            for e in data["entities"]
        ],
    )


def _ensure_editable(preview: RedactionPreview, state: SettingsState) -> None:
    if preview.status is not RedactionPreviewStatus.ready:
        raise ConflictError(
            "Предпросмотр нельзя изменить в текущем состоянии", code="redaction_preview_not_editable"
        )
    reason = _stale_reason(preview, state)
    if reason == "expired":
        raise ConflictError(
            "Срок предпросмотра истёк — проверьте маскировку заново", code="redaction_preview_expired"
        )
    if reason == "settings_changed":
        raise ConflictError(
            "Настройки маскировки изменились — проверьте маскировку заново", code="redaction_settings_changed"
        )


def _find_entity(entities: list[dict[str, Any]], entity_id: str) -> dict[str, Any]:
    for e in entities:
        if e["id"] == entity_id:
            return e
    raise AppError("Сущность не найдена", code="redaction_entity_not_found", status_code=422)


def _add_mark(data: dict[str, Any], texts: dict[str, str], op: AddMarkOp) -> None:
    if op.category not in CODES or not BY_CODE[op.category].supported:
        raise AppError("Неизвестная категория", code="redaction_invalid_category", status_code=422)
    text_ = texts.get(op.fragment)
    if text_ is None or op.end > len(text_) or op.start >= op.end or op.end - op.start > MAX_MARK_CHARS:
        raise AppError("Некорректные границы пометки", code="redaction_invalid_mark", status_code=422)
    value = text_[op.start : op.end]
    if not value.strip():
        raise AppError("Пометка не может быть пустой", code="redaction_invalid_mark", status_code=422)
    entities: list[dict[str, Any]] = data["entities"]
    spans: list[tuple[str, int, int]] = [(op.fragment, op.start, op.end)]
    if op.all_occurrences:
        pattern = re.compile(r"(?<![\wА-Яа-яЁё])" + re.escape(value) + r"(?![\wА-Яа-яЁё])")
        for key, body in texts.items():
            for m in pattern.finditer(body):
                if (key, m.start(), m.end()) != spans[0]:
                    spans.append((key, m.start(), m.end()))
    occupied = [(o["fragment"], o["start"], o["end"], e, o) for e in entities for o in e["occurrences"]]
    accepted: list[tuple[str, int, int]] = []
    for key, start, end in spans:
        overlapping = [x for x in occupied if x[0] == key and x[1] < end and start < x[2]]
        if any(not (start <= x[1] and x[2] <= end) for x in overlapping):
            if (key, start, end) == spans[0]:
                raise ConflictError(
                    "Пометка частично пересекается с найденным значением — снимите его или выделите целиком",
                    code="redaction_mark_overlaps",
                )
            continue  # прочие вхождения с частичным пересечением пропускаем
        # Вложенные найденные вхождения поглощаются новой пометкой.
        for _k, _s, _e, entity, occurrence in overlapping:
            entity["occurrences"].remove(occurrence)
        accepted.append((key, start, end))
    data["entities"] = [e for e in entities if e["occurrences"]]
    number = int(data.get("next_manual", 1))
    data["next_manual"] = number + 1
    entity_id = f"M{number:04d}"
    data["entities"].append(
        {
            "id": entity_id,
            "key": f"manual:{number}",
            "category": op.category,
            "canonical": " ".join(value.split()),
            "confidence": "high",
            "source": "manual",
            "masked": True,
            "occurrences": [
                {
                    "id": f"{entity_id}.{n}",
                    "fragment": key,
                    "start": start,
                    "end": end,
                    "text": texts[key][start:end],
                    "source": "manual",
                    "masked": True,
                }
                for n, (key, start, end) in enumerate(sorted(accepted), start=1)
            ],
        }
    )


async def edit_preview(
    db: AsyncSession, box: SecretBox, preview: RedactionPreview, ops: list[EditOp]
) -> RedactionPreview:
    """Правки пользователя (вызывается с заблокированной строкой предпросмотра)."""
    state = await get_state(db)
    _ensure_editable(preview, state)
    assert preview.entities_encrypted is not None
    data = _decrypt(box, preview.entities_encrypted)
    texts: dict[str, str] | None = None
    for op in ops:
        if isinstance(op, SetEntityOp):
            _find_entity(data["entities"], op.entity_id)["masked"] = op.masked
        elif isinstance(op, SetOccurrenceOp):
            entity_id = op.occurrence_id.split(".", 1)[0]
            entity = _find_entity(data["entities"], entity_id)
            for o in entity["occurrences"]:
                if o["id"] == op.occurrence_id:
                    o["masked"] = op.masked
                    break
            else:
                raise AppError("Вхождение не найдено", code="redaction_entity_not_found", status_code=422)
        elif isinstance(op, SetCategoryOp):
            if op.category not in CODES or not BY_CODE[op.category].supported:
                raise AppError("Неизвестная категория", code="redaction_invalid_category", status_code=422)
            _find_entity(data["entities"], op.entity_id)["category"] = op.category
        else:
            if texts is None:
                rows = (
                    await db.execute(
                        select(DocumentFragment.fragment_key, DocumentFragment.text).where(
                            DocumentFragment.version_id == preview.version_id
                        )
                    )
                ).all()
                texts = {str(k): str(t) for k, t in rows}
            _add_mark(data, texts, op)
    preview.entities_encrypted, preview.secret_key_id = _encrypt(box, data)
    preview.counts = _preview_counts(data["entities"], ner=data.get("ner"))
    await db.commit()
    await db.refresh(preview)
    return preview


async def confirm_preview(
    db: AsyncSession, actor: User, ip: str | None, preview: RedactionPreview, body: PreviewConfirmIn
) -> RedactionPreview:
    state = await get_state(db)
    if await _is_used(db, preview.id):
        raise ConflictError("Предпросмотр уже использован для анализа", code="redaction_preview_used")
    if preview.status is RedactionPreviewStatus.confirmed:
        # Повторное подтверждение (например, после ошибки запуска) — разрешено с тем же или новым режимом.
        preview.status = RedactionPreviewStatus.ready
    _ensure_editable(preview, state)
    if not body.reviewed:
        raise AppError(
            "Отметьте, что подсвеченные данные проверены", code="redaction_review_required", status_code=422
        )
    unmasked = body.mode == "unmasked"
    now = _now()
    if unmasked:
        if not state.settings.allow_unmasked:
            raise PermissionDenied(
                "Отправка без маскировки запрещена настройками", code="redaction_unmasked_not_allowed"
            )
        if not body.confirm_unmasked or body.unmasked_notice_version != UNMASKED_NOTICE_VERSION:
            raise AppError(
                "Подтвердите отправку текста без маскировки",
                code="redaction_unmasked_confirmation_required",
                status_code=422,
                details={"notice_version": UNMASKED_NOTICE_VERSION},
            )
    preview.status = RedactionPreviewStatus.confirmed
    preview.mode = RedactionMode.unmasked_by_user if unmasked else RedactionMode.masked
    preview.confirmed_at = now
    preview.unmasked_confirmed_at = now if unmasked else None
    counts = dict(preview.counts or {})
    record(
        db,
        AuditAction.redaction_preview_confirmed,
        actor_id=actor.id,
        target_type="document",
        target_id=preview.document_id,
        ip=ip,
        details={
            "preview_id": str(preview.id),
            "version_id": str(preview.version_id),
            "mode": preview.mode.value,
            "categories": counts.get("categories", {}),
            "values": counts.get("values", 0),
            "manual_added": counts.get("manual", 0),
            "unmarked": counts.get("unmarked", 0),
        },
    )
    await db.commit()
    await db.refresh(preview)
    return preview


# ================================================================== запуск анализа


@dataclass
class ConsumedPreview:
    preview: RedactionPreview
    mode: RedactionMode
    mapping: dict[str, Any] | None
    counts: dict[str, Any]
    settings_revision: int


async def consume_preview(
    db: AsyncSession,
    actor: Actor,
    box: SecretBox,
    *,
    preview_id: uuid.UUID,
    version: DocumentVersion,
) -> ConsumedPreview:
    """Проверки предпросмотра при запуске облачного анализа (в транзакции запуска)."""
    from app.security.scoped import get_redaction_preview_for  # noqa: PLC0415

    preview = await get_redaction_preview_for(db, actor, preview_id, for_update=True)
    if preview.version_id != version.id:
        raise AppError(
            "Предпросмотр маскировки выполнен для другой версии документа",
            code="redaction_preview_mismatch",
            status_code=422,
        )
    if preview.status is not RedactionPreviewStatus.confirmed or preview.mode is None:
        raise AppError(
            "Проверьте и подтвердите маскировку перед облачным анализом",
            code="redaction_preview_required",
            status_code=422,
        )
    if await _is_used(db, preview.id):
        raise ConflictError("Предпросмотр уже использован для анализа", code="redaction_preview_used")
    state = await get_state(db)
    reason = _stale_reason(preview, state)
    if reason == "expired":
        raise ConflictError(
            "Срок предпросмотра истёк — проверьте маскировку заново", code="redaction_preview_expired"
        )
    if reason == "settings_changed":
        raise ConflictError(
            "Настройки маскировки изменились — проверьте маскировку заново", code="redaction_settings_changed"
        )
    if preview.mode is RedactionMode.unmasked_by_user and not state.settings.allow_unmasked:
        raise PermissionDenied(
            "Отправка без маскировки запрещена настройками", code="redaction_unmasked_not_allowed"
        )
    _require_box(box)
    assert preview.entities_encrypted is not None
    data = _decrypt(box, preview.entities_encrypted)
    mapping = build_mapping(data["entities"], await _fragment_seq(db, version.id))
    counts = mapping_counts(mapping)
    return ConsumedPreview(
        preview,
        preview.mode,
        mapping if preview.mode is RedactionMode.masked else None,
        counts,
        preview.settings_revision,
    )


def masked_sizes(mapping: dict[str, Any], sizes: dict[str, int]) -> dict[str, int]:
    """Длины фрагментов после замены значений метками (для планирования чанков)."""
    result = dict(sizes)
    for key, items in mapping["replacements"].items():
        if key in result:
            result[key] += sum(len(label) - (int(end) - int(start)) for start, end, label in items)
    return result


def attach(
    db: AsyncSession, box: SecretBox, analysis_id: uuid.UUID, consumed: ConsumedPreview
) -> AnalysisRedaction:
    """Таблица соответствий анализа; найденные данные предпросмотра больше не нужны — удаляются."""
    ciphertext: bytes | None = None
    key_id: str | None = None
    if consumed.mapping is not None:
        ciphertext, key_id = _encrypt(box, consumed.mapping)
    row = AnalysisRedaction(
        analysis_id=analysis_id,
        preview_id=consumed.preview.id,
        mode=consumed.mode,
        unmasked_confirmed_at=consumed.preview.unmasked_confirmed_at,
        mapping_encrypted=ciphertext,
        secret_key_id=key_id,
        counts=consumed.counts,
        redaction_version=consumed.preview.redaction_version,
        settings_revision=consumed.settings_revision,
    )
    db.add(row)
    consumed.preview.entities_encrypted = None
    consumed.preview.secret_key_id = None
    return row


async def load_mapping(db: AsyncSession, box: SecretBox, analysis_id: uuid.UUID) -> dict[str, Any] | None:
    """Таблица соответствий анализа в режиме masked (иначе None)."""
    row = await db.get(AnalysisRedaction, analysis_id)
    if row is None or row.mode is not RedactionMode.masked or row.mapping_encrypted is None:
        return None
    return _decrypt(box, row.mapping_encrypted)


# ================================================================== обслуживание


async def delete_expired_previews(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Просроченные предпросмотры, не связанные с анализом, удаляются (временные данные)."""
    async with session_factory() as db:
        used = select(AnalysisRedaction.preview_id).where(AnalysisRedaction.preview_id.is_not(None))
        result = await db.execute(
            delete(RedactionPreview)
            .where(RedactionPreview.expires_at < func.now(), RedactionPreview.id.not_in(used))
            .returning(RedactionPreview.id)
        )
        count = len(result.all())
        await db.commit()
    if count:
        log.info("redaction_previews_expired", count=count)
    return count


async def rotate_secrets(db: AsyncSession, box: SecretBox) -> tuple[int, int]:
    """Перешифровывает найденные данные предпросмотров и таблицы соответствий основным ключом."""
    rotated = failed = 0
    targets: list[tuple[Any, Any, Any, Any]] = [
        (
            RedactionPreview,
            RedactionPreview.id,
            RedactionPreview.entities_encrypted,
            RedactionPreview.secret_key_id,
        ),
        (
            AnalysisRedaction,
            AnalysisRedaction.analysis_id,
            AnalysisRedaction.mapping_encrypted,
            AnalysisRedaction.secret_key_id,
        ),
    ]
    for model, pk, column, key_column in targets:
        rows = (
            await db.execute(select(pk, column, key_column).where(column.is_not(None)).with_for_update())
        ).all()
        for row_id, ciphertext, key_id in rows:
            if ciphertext is None or key_id == box.primary_key_id:
                continue
            try:
                new_ciphertext, new_key_id = box.rotate(ciphertext)
            except AppError:
                failed += 1
                continue
            await db.execute(
                update(model)
                .where(pk == row_id)
                .values({column.key: new_ciphertext, "secret_key_id": new_key_id})
                .execution_options(synchronize_session=False)
            )
            rotated += 1
    await db.commit()
    return rotated, failed
