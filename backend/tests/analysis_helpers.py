"""Помощники тестов анализа: синтетический договор в БД, модель в каталоге, сценарная модель, запуск
задач через настоящий worker (claim → handler → complete/fail)."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.jobs import queue
from app.llm.base import ProviderHealth, RemoteModelInfo, StructuredRequest, StructuredResult, Usage
from app.llm.errors import ModelUnavailable, ProviderError
from app.llm.factory import ProviderFactory
from app.models.catalog import ModelConfig, ProviderConfig
from app.models.documents import Document, DocumentFragment, DocumentPage, DocumentVersion
from app.models.enums import (
    DocumentFormat,
    ExtractionStatus,
    FragmentKind,
    PageSource,
    ProviderType,
    StructuredOutputSupport,
    UserRole,
)
from app.models.users import ConsentRecord, User
from app.security.secretbox import SecretBox
from app.worker import Worker
from tests.integration.conftest import ApiEnv

# (вид, текст, пункт, страница) — синтетический договор без реальных данных.
CONTRACT: list[tuple[str, str, str | None, int]] = [
    ("heading", "ДОГОВОР ОКАЗАНИЯ УСЛУГ № 17", None, 1),
    (
        "paragraph",
        "ООО «Альфа», именуемое в дальнейшем «Заказчик», с одной стороны, и ООО «Бета», именуемое в "
        "дальнейшем «Исполнитель», с другой стороны, заключили настоящий договор о нижеследующем.",
        None,
        1,
    ),
    ("heading", "1. Предмет договора", "1", 1),
    (
        "list_item",
        "1.1. Исполнитель обязуется оказать услуги по сопровождению информационной системы, а Заказчик "
        "обязуется оплатить эти услуги.",
        "1.1",
        1,
    ),
    ("heading", "2. Стоимость и порядок оплаты", "2", 1),
    (
        "list_item",
        "2.1. Заказчик оплачивает услуги в течение 5 рабочих дней с даты подписания акта.",
        "2.1",
        1,
    ),
    (
        "list_item",
        "2.2. Исполнитель вправе в одностороннем порядке изменить стоимость услуг, уведомив Заказчика "
        "за 3 календарных дня.",
        "2.2",
        1,
    ),
    ("heading", "3. Ответственность сторон", "3", 2),
    (
        "list_item",
        "3.1. За просрочку оплаты Заказчик уплачивает неустойку в размере 1 % от суммы задолженности "
        "за каждый день просрочки.",
        "3.1",
        2,
    ),
    ("list_item", "3.2. Ответственность Исполнителя по договору ограничена суммой 1000 рублей.", "3.2", 2),
    ("heading", "4. Срок действия", "4", 2),
    (
        "list_item",
        "4.1. Договор действует до 31.12.2026 и автоматически продлевается на каждый следующий год.",
        "4.1",
        2,
    ),
    (
        "list_item",
        "4.2. Оплата услуг производится в течение 30 календарных дней с даты подписания акта.",
        "4.2",
        2,
    ),
]


def key(seq: int) -> str:
    return f"F{seq:06d}"


def text_of(seq: int, contract: list[tuple[str, str, str | None, int]] = CONTRACT) -> str:
    return contract[seq][1]


@dataclass
class Doc:
    document_id: uuid.UUID
    version_id: uuid.UUID
    fragment_ids: list[uuid.UUID]


async def make_document(
    api: ApiEnv,
    owner: User,
    contract: list[tuple[str, str, str | None, int]] | None = None,
    *,
    page_source: PageSource | None = PageSource.pdf_native,
    extraction_status: ExtractionStatus = ExtractionStatus.ready,
) -> Doc:
    """Готовая версия документа с фрагментами и страницами — как после этапа извлечения (§8.5)."""
    contract = CONTRACT if contract is None else contract
    document_id, version_id = uuid.uuid4(), uuid.uuid4()
    fragments: list[DocumentFragment] = []
    pages: dict[int, list[int]] = {}
    pos = 0
    for seq, (kind, body, clause, page) in enumerate(contract):
        if seq:
            pos += 2
        fragments.append(
            DocumentFragment(
                id=uuid.uuid4(),
                version_id=version_id,
                seq=seq,
                fragment_key=key(seq),
                kind=FragmentKind(kind),
                text=body,
                text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                char_start=pos,
                char_end=pos + len(body),
                page_start=page if page_source else None,
                page_end=page if page_source else None,
                clause_ref=clause,
                heading_path=[],
                meta={},
            )
        )
        pages.setdefault(page, []).append(pos)
        pos += len(body)
    async with api.factory() as db:
        db.add(Document(id=document_id, owner_id=owner.id, title="Тестовый договор"))
        await db.flush()
        db.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=1,
                original_filename="contract.pdf",
                detected_format=DocumentFormat.pdf,
                mime_type="application/pdf",
                size_bytes=1000,
                sha256="0" * 64,
                storage_key=f"originals/{uuid.uuid4().hex}",
                extraction_status=extraction_status,
                page_source=page_source,
                page_count=len(pages) if page_source else None,
                uploaded_by_id=owner.id,
            )
        )
        await db.flush()
        await db.execute(
            text("UPDATE documents SET current_version_id = :v WHERE id = :d"),
            {"v": version_id, "d": document_id},
        )
        db.add_all(fragments)
        if page_source:
            numbers = sorted(pages)
            for i, page_no in enumerate(numbers):
                start = pages[page_no][0]
                end = pages[numbers[i + 1]][0] - 2 if i + 1 < len(numbers) else pos
                db.add(
                    DocumentPage(
                        version_id=version_id,
                        page_no=page_no,
                        char_start=start,
                        char_end=end,
                        source=page_source,
                    )
                )
        await db.commit()
    return Doc(document_id, version_id, [f.id for f in fragments])


async def make_model(
    api: ApiEnv,
    *,
    cloud: bool = False,
    name: str | None = None,
    pricing: dict[str, Any] | None = None,
    context_window: int | None = 32_000,
    max_output_tokens: int | None = 4096,
    purpose: list[str] | None = None,
    roles: list[str] | None = None,
    enabled: bool = True,
) -> tuple[ProviderConfig, ModelConfig]:
    suffix = uuid.uuid4().hex[:6]
    provider = ProviderConfig(
        id=uuid.uuid4(),
        name=name or f"Провайдер {suffix}",
        type=ProviderType.openrouter if cloud else ProviderType.ollama,
        base_url="https://openrouter.ai/api/v1" if cloud else "http://ollama:11434",
        is_cloud=cloud,
        enabled=True,
        allow_private_network=not cloud,
        timeout_s=60,
        max_concurrency=2,
        extra_headers={},
    )
    model = ModelConfig(
        id=uuid.uuid4(),
        provider_config_id=provider.id,
        display_name=f"Модель {suffix}",
        external_model_id=f"test/model-{suffix}",
        enabled=enabled,
        purpose=purpose or ["analysis", "verifier"],
        allowed_roles=roles or [r.value for r in UserRole if r is not UserRole.viewer],
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        generation_params={"temperature": 0},
        structured_output=StructuredOutputSupport.json_schema,
        pricing=pricing,
    )
    async with api.factory() as db:
        db.add(provider)
        await db.flush()
        db.add(model)
        await db.commit()
    return provider, model


async def enable_cloud(api: ApiEnv, enabled: bool = True) -> None:
    await api.execute(
        "INSERT INTO system_settings (key, value) VALUES ('cloud_models_enabled', CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        v="true" if enabled else "false",
    )


async def accept_notice(api: ApiEnv, user: User, provider: ProviderConfig) -> None:
    from app.services.catalog import cloud_notice

    async with api.factory() as db:
        notice = await cloud_notice(db)
        db.add(ConsentRecord(user_id=user.id, provider_config_id=provider.id, notice_version=notice.version))
        await db.commit()


# ------------------------------------------------------------------ сценарная модель

Handler = Callable[[StructuredRequest], dict[str, Any]] | dict[str, Any] | ProviderError | list[Any]
_FRAGMENT_ID = re.compile(r'<fragment id="(F\d{6})"')
_EVIDENCE_NO = re.compile(r'<evidence no="(\d+)">')


def fragment_keys(req: StructuredRequest) -> list[str]:
    return _FRAGMENT_ID.findall(req.messages[-1].content if req.messages else "")


def user_text(req: StructuredRequest) -> str:
    return "\n".join(m.content for m in req.messages if m.role == "user")


@dataclass
class FakeLLM:
    """Ответ выбирается по имени схемы запроса. Значение-список — очередь ответов (последний повторяется)."""

    handlers: dict[str, Handler] = field(default_factory=dict)
    calls: list[StructuredRequest] = field(default_factory=list)
    usage: Usage | None = field(default_factory=lambda: Usage(prompt_tokens=100, completion_tokens=50))
    hook: Callable[[StructuredRequest], Any] | None = None

    def by_schema(self, name: str) -> list[StructuredRequest]:
        return [c for c in self.calls if c.schema_name == name]

    async def generate_structured(self, req: StructuredRequest) -> StructuredResult:
        self.calls.append(req)
        if self.hook is not None:
            await self.hook(req)
        handler: Any = self.handlers.get(req.schema_name, DEFAULTS.get(req.schema_name))
        if isinstance(handler, list):
            handler = handler.pop(0) if len(handler) > 1 else handler[0]
        if isinstance(handler, ProviderError):
            raise handler
        payload = handler(req) if callable(handler) else handler
        if isinstance(payload, ProviderError):
            raise payload
        return StructuredResult(
            parsed_json=payload,
            raw_text_len=len(str(payload)),
            usage=self.usage,
            provider_request_id=f"fake-{len(self.calls)}",
            finish_reason="stop",
            model=req.model,
        )

    async def check_connection(self) -> ProviderHealth:
        return ProviderHealth(ok=True, status="ok", message="fake")

    async def list_models(self) -> list[RemoteModelInfo]:
        return []

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        raise ModelUnavailable()


def verdicts(verdict: str = "supports") -> Callable[[StructuredRequest], dict[str, Any]]:
    def handler(req: StructuredRequest) -> dict[str, Any]:
        numbers = [int(n) for n in _EVIDENCE_NO.findall(user_text(req))]
        return {
            "verdicts": [{"evidence_no": n, "verdict": verdict, "rationale": "Проверено"} for n in numbers]
        }

    return handler


EMPTY: dict[str, Any] = {"risks": [], "notes": None}
DEFAULTS: dict[str, Any] = {
    "contract_structure": {
        "contract_type": "договор оказания услуг",
        "subject": "сопровождение информационной системы",
        "parties": [
            {"label": "party_a", "name": "ООО «Альфа»", "role": "Заказчик"},
            {"label": "party_b", "name": "ООО «Бета»", "role": "Исполнитель"},
        ],
    },
    "chunk_risks": EMPTY,
    "cross_section_risks": EMPTY,
    "quote_retry": {"withdraw": True, "evidence": [], "comment": None},
    "evidence_relevance": verdicts("supports"),
}


def risk(
    title: str,
    evidence: list[tuple[str, str]],
    *,
    category: str = "unilateral_changes",
    kind: str = "clause_risk",
    severity: str = "high",
    description: str = "Описание риска",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "category": category,
        "kind": kind,
        "severity": severity,
        "description": description,
        "reason": "Условие позволяет одной стороне менять обязательства без согласия другой",
        "consequences": "Рост расходов",
        "affected_party": "party_a",
        "recommendation": "Согласовать изменение стоимости только дополнительным соглашением",
        "suggested_revision": None,
        "model_confidence": 0.8,
        "evidence": [{"fragment_id": k, "quote": q} for k, q in evidence],
        "search_scope": None,
    }
    item.update(extra)
    return item


def risks(*items: dict[str, Any]) -> dict[str, Any]:
    return {"risks": list(items), "notes": None}


def by_fragment(mapping: dict[str, list[dict[str, Any]]]) -> Callable[[StructuredRequest], dict[str, Any]]:
    """Ответ анализа чанка: риски для фрагментов, которые есть в этом чанке."""

    def handler(req: StructuredRequest) -> dict[str, Any]:
        present = set(fragment_keys(req))
        found = [r for k, items in mapping.items() if k in present for r in items]
        return {"risks": found, "notes": None}

    return handler


def factory_for(api: ApiEnv, fake: FakeLLM) -> ProviderFactory:
    return ProviderFactory(SecretBox(api.settings.secrets_encryption_keys), override=lambda _cfg: fake)


async def run_jobs(
    api: ApiEnv,
    fake: FakeLLM,
    *,
    limit: int = 1,
    release_delayed: bool = True,
    job_types: tuple[str, ...] = ("run_analysis",),
) -> int:
    """Выполняет до `limit` задач (по умолчанию `run_analysis`) настоящим worker'ом.

    Возвращает число выполненных задач.
    """
    worker = Worker(api.settings, api.factory, factory_for(api, fake))
    done = 0
    for _ in range(limit):
        if release_delayed:
            await api.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued'")
        async with api.factory() as session:
            job = await queue.claim(session, worker.worker_id, 120, list(job_types))
        if job is None:
            break
        await worker.execute(job)
        done += 1
    return done


async def start(client: Any, doc: Doc, model: ModelConfig, **extra: Any) -> dict[str, Any]:
    r = await client.post("/api/analyses", json=analysis_body(doc, model, **extra))
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def get_analysis(client: Any, analysis_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/analyses/{analysis_id}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def risk_list(client: Any, analysis_id: str, **params: Any) -> dict[str, Any]:
    r = await client.get(f"/api/analyses/{analysis_id}/risks", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def analysis_body(doc: Doc, model: ModelConfig, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "document_id": str(doc.document_id),
        "party": "party_a",
        "categories": [
            "payment",
            "liability",
            "penalties",
            "deadlines",
            "renewal",
            "termination",
            "unilateral_changes",
            "contradiction",
            "missing_clause",
            "other",
        ],
        "depth": "standard",
        "model_id": str(model.id),
    }
    body.update(extra)
    return body


# ------------------------------------------------------------------ маскировка (этап 10)


async def create_preview(client: Any, doc: Doc) -> dict[str, Any]:
    r = await client.post(f"/api/documents/{doc.document_id}/versions/{doc.version_id}/redaction-previews")
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def scan_previews(api: ApiEnv, limit: int = 1) -> int:
    """Выполняет задачи `redaction_scan` настоящим worker'ом (обнаружение — в дочернем процессе)."""
    return await run_jobs(api, FakeLLM(), limit=limit, job_types=("redaction_scan",))


async def confirmed_preview(api: ApiEnv, client: Any, doc: Doc, **confirm: Any) -> str:
    """Предпросмотр маскировки: создать → выполнить поиск → подтвердить. Возвращает ID."""
    preview = await create_preview(client, doc)
    await scan_previews(api)
    r = await client.post(
        f"/api/redaction-previews/{preview['id']}/confirm", json={"reviewed": True, **confirm}
    )
    assert r.status_code == 200, r.text
    preview_id: str = preview["id"]
    return preview_id
