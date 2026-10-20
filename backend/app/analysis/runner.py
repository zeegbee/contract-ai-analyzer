"""Выполнение анализа в worker'е (задача `run_analysis`, ARCHITECTURE §9.2, §13).

Этапы (каждый сохраняет результат, поэтому после сбоя задача продолжает с незавершённого):

    structure → chunks (N) → cross_section → materialize (dedup + шаг A проверки доказательств)
    → quote_retry → verify (шаг B) → finalize (статусы, покрытие, сводка)

- Отмена: флаг `cancel_requested_at` проверяется перед каждым вызовом модели и во время вызова
  (текущий HTTP-запрос отменяется); уже полученные результаты сохраняются.
- Повтор: временные ошибки провайдера → повтор задачи (готовые чанки не пересчитываются);
  на последней попытке и при неустранимых ошибках → `failed` с безопасным кодом.
- Fencing: каждая запись начинается с блокировки строки анализа и проверки `progress.run_id` —
  если задачу перехватил другой worker (истёк lease), старый исполнитель ничего не запишет.
- Перед каждым вызовом облачной модели заново проверяется, что облачные модели и провайдер
  включены (аварийное отключение облака останавливает уже запущенные анализы).
- В логи пишутся только метаданные (этап, номер чанка, коды, токены) — без текста договора.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import prompts
from app.analysis.chunking import CHARS_PER_TOKEN, DEFAULT_CONTEXT_TOKENS, estimate_tokens
from app.analysis.dedup import SEVERITY_RANK, Candidate, deduplicate
from app.analysis.errors import safe_message
from app.analysis.evidence import (
    MATCHED,
    SEPARATOR,
    EvidenceMatch,
    EvidenceState,
    FragmentIndex,
    FragmentRef,
    SeqScope,
    apply_relevance_rules,
    final_status,
    match_evidence,
)
from app.analysis.injection import scan
from app.analysis.report import ChunkInfo, RiskInfo, build_coverage, build_summary
from app.analysis.schemas import (
    QuoteRetryOut,
    RiskItem,
    RiskList,
    StructureOut,
    VerifierOut,
    provider_schema,
)
from app.analysis.structure import build_sections, edge_fragments, key_fragments, merge_llm_structure, outline
from app.config import Settings
from app.jobs.registry import JobError
from app.llm.base import ChatMessage, GenerationParams, LLMProvider, StructuredRequest, StructuredResult
from app.llm.errors import ContextTooLong, InvalidModelOutput, ProviderError
from app.llm.factory import ProviderFactory
from app.logging import bind_context, get_logger
from app.models.analyses import Analysis, AnalysisChunk, AnalysisRisk, RiskEvidence
from app.models.catalog import ModelConfig, ProviderConfig
from app.models.documents import Document, DocumentFragment, DocumentPage, DocumentVersion
from app.models.enums import (
    AnalysisDepth,
    AnalysisParty,
    AnalysisStatus,
    ChunkStatus,
    DocumentStatus,
    PageSource,
    ProviderType,
    RelevanceStatus,
    RiskCategory,
    RiskKind,
    StructuredOutputSupport,
    ValidationStatus,
)
from app.services.catalog import cloud_models_enabled

log = get_logger(__name__)

TERMINAL = frozenset(
    {
        AnalysisStatus.completed,
        AnalysisStatus.completed_with_warnings,
        AnalysisStatus.failed,
        AnalysisStatus.cancelled,
    }
)
MAX_RISK_CARDS = 80
VERIFIER_CONTEXT_CHARS = 1500
_LEGAL_REFERENCE = re.compile(
    r"(\bст\.\s*\d|\bстать[яиею]\s+\d|\bГК\s+РФ\b|\bАПК\b|\bГПК\b|\bфедеральн\w+\s+закон|\b\d+-ФЗ\b"
    r"|\bкодекс\w*\b|\bпостановлени\w+\s+пленума)",
    re.IGNORECASE,
)
_ADDRESSES_READER = re.compile(
    r"\b(для\s+вас|вам\s+(выгодн|невыгодн)|вы\s+(рискуете|несёте|несете))", re.IGNORECASE
)

T = TypeVar("T", bound=BaseModel)


class AnalysisCancelled(Exception):
    pass


class Superseded(Exception):
    """Анализ удалён или выполняется другим исполнителем — ничего не записываем."""


class AnalysisFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetryLater(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutputError(Exception):
    """Вызов модели не дал корректного результата для этапа (этап продолжается без него).

    fatal=True — модель/провайдер недоступны (для верификатора: больше не вызывать).
    """

    def __init__(self, code: str, *, fatal: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal


@dataclass
class Target:
    """Модель, которой отправляются запросы (снимок конфигурации + собранный провайдер)."""

    llm: LLMProvider
    provider_id: uuid.UUID
    provider_type: ProviderType
    is_cloud: bool
    external_model_id: str
    context_window: int | None
    max_output_tokens: int
    params: dict[str, Any]
    structured_output: StructuredOutputSupport
    snapshot: dict[str, Any]


@dataclass
class Generated[M: BaseModel]:
    value: M
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass
class _Chunk:
    id: uuid.UUID
    index: int
    seq_start: int
    seq_end: int
    status: ChunkStatus
    error_code: str | None
    result: dict[str, Any] | None


def _now() -> datetime:
    return datetime.now(UTC)


def _validation_errors(exc: ValidationError) -> list[str]:
    # Только путь и тип ошибки — без значений (там может быть текст договора).
    return [f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['type']}" for e in exc.errors()[:10]]


class AnalysisRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        llm_factory: ProviderFactory,
        analysis_id: uuid.UUID,
        *,
        last_attempt: bool,
    ) -> None:
        self.settings = settings
        self.factory = session_factory
        self.llm_factory = llm_factory
        self.analysis_id = analysis_id
        self.last_attempt = last_attempt
        self.run_id = uuid.uuid4().hex
        self.usage: dict[str, Any] = {}
        self.stage_results: dict[str, Any] = {}

    # ================================================================ точка входа

    async def run(self) -> None:
        bind_context(analysis_id=str(self.analysis_id))
        if not await self._start():
            return
        try:
            await self._prepare()
            await self._scan_injection()
            await self._structure()
            await self._chunks()
            await self._cross_section()
            await self._materialize()
            await self._quote_retries()
            await self._verify()
            await self._finalize()
        except AnalysisCancelled:
            await self._mark_cancelled()
        except Superseded:
            log.warning("analysis_superseded")
        except AnalysisFailed as exc:
            await self._mark_failed(exc.code)
            raise JobError(exc.code, retryable=False) from None
        except RetryLater as exc:
            await self._mark_retry(exc.code)
            raise JobError(exc.code, retryable=True) from None
        except Exception:
            if self.last_attempt:
                with contextlib.suppress(Exception):
                    await self._mark_failed("internal_error")
            raise

    # ================================================================ транзакции и состояние

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[tuple[AsyncSession, Analysis]]:
        async with self.factory() as db:
            analysis = (
                await db.execute(select(Analysis).where(Analysis.id == self.analysis_id).with_for_update())
            ).scalar_one_or_none()
            if analysis is None or (analysis.progress or {}).get("run_id") != self.run_id:
                raise Superseded()
            yield db, analysis
            analysis.usage = dict(self.usage)
            analysis.stage_results = dict(self.stage_results)
            await db.commit()

    def _set_progress(self, analysis: Analysis, **values: Any) -> None:
        progress = dict(analysis.progress or {})
        progress.update(values)
        analysis.progress = progress

    async def _start(self) -> bool:
        async with self.factory() as db:
            analysis = (
                await db.execute(select(Analysis).where(Analysis.id == self.analysis_id).with_for_update())
            ).scalar_one_or_none()
            if analysis is None or analysis.status in TERMINAL:
                await db.rollback()
                return False
            doc_status = (
                await db.execute(select(Document.status).where(Document.id == analysis.document_id))
            ).scalar_one_or_none()
            if doc_status is not DocumentStatus.active:
                await db.rollback()
                return False
            if analysis.cancel_requested_at is not None or analysis.status is AnalysisStatus.cancelling:
                analysis.status = AnalysisStatus.cancelled
                analysis.finished_at = _now()
                self._set_progress(analysis, stage="cancelled")
                await db.commit()
                log.info("analysis_cancelled_before_start")
                return False
            if analysis.status is AnalysisStatus.queued:
                analysis.status = AnalysisStatus.running
            analysis.started_at = analysis.started_at or _now()
            self._set_progress(analysis, run_id=self.run_id, retry_scheduled=None)
            self.usage = dict(analysis.usage or {})
            self.stage_results = dict(analysis.stage_results or {})
            self.snapshot: dict[str, Any] = dict(analysis.config_snapshot)
            self.version_id = analysis.version_id
            self.party = analysis.party
            self.party_name = analysis.party_name
            self.categories = [RiskCategory(c) for c in analysis.categories]
            self.depth = analysis.depth
            self.cloud_transfer = analysis.cloud_transfer
            await db.commit()
        log.info("analysis_started_run")
        return True

    async def _mark_cancelled(self) -> None:
        async with self.factory() as db:
            analysis = (
                await db.execute(select(Analysis).where(Analysis.id == self.analysis_id).with_for_update())
            ).scalar_one_or_none()
            if analysis is None or analysis.status in TERMINAL:
                return
            await db.execute(
                update(AnalysisChunk)
                .where(
                    AnalysisChunk.analysis_id == self.analysis_id, AnalysisChunk.status == ChunkStatus.running
                )
                .values(status=ChunkStatus.pending)
            )
            analysis.status = AnalysisStatus.cancelled
            analysis.finished_at = _now()
            analysis.usage = dict(self.usage)
            self._set_progress(analysis, stage="cancelled")
            await db.commit()
        log.info("analysis_cancelled")

    async def _mark_failed(self, code: str) -> None:
        async with self.factory() as db:
            analysis = (
                await db.execute(select(Analysis).where(Analysis.id == self.analysis_id).with_for_update())
            ).scalar_one_or_none()
            if analysis is None or analysis.status in TERMINAL:
                return
            if (analysis.progress or {}).get("run_id") not in (self.run_id, None):
                return
            await db.execute(
                update(AnalysisChunk)
                .where(
                    AnalysisChunk.analysis_id == self.analysis_id, AnalysisChunk.status == ChunkStatus.running
                )
                .values(status=ChunkStatus.pending)
            )
            analysis.status = AnalysisStatus.failed
            analysis.error_code = code[:64]
            analysis.error_message_safe = safe_message(code)[:500]
            analysis.finished_at = _now()
            analysis.usage = dict(self.usage)
            self._set_progress(analysis, stage="failed")
            await db.commit()
        log.warning("analysis_failed", code=code)

    async def _mark_retry(self, code: str) -> None:
        with contextlib.suppress(Superseded):
            async with self._tx() as (db, analysis):
                await db.execute(
                    update(AnalysisChunk)
                    .where(
                        AnalysisChunk.analysis_id == self.analysis_id,
                        AnalysisChunk.status == ChunkStatus.running,
                    )
                    .values(status=ChunkStatus.pending)
                )
                self._set_progress(analysis, retry_scheduled=code)
        log.warning("analysis_retry_scheduled", code=code)

    # ================================================================ подготовка

    async def _prepare(self) -> None:
        async with self.factory() as db:
            version = (
                await db.execute(select(DocumentVersion).where(DocumentVersion.id == self.version_id))
            ).scalar_one()
            self.page_source: PageSource | None = version.page_source
            rows = (
                await db.execute(
                    select(DocumentFragment)
                    .where(DocumentFragment.version_id == self.version_id)
                    .order_by(DocumentFragment.seq)
                )
            ).scalars()
            fragments = [
                FragmentRef(
                    id=f.id,
                    seq=f.seq,
                    key=f.fragment_key,
                    kind=f.kind.value,
                    text=f.text,
                    char_start=f.char_start,
                    char_end=f.char_end,
                    page_start=f.page_start,
                    page_end=f.page_end,
                    clause_ref=f.clause_ref,
                    heading_path=tuple(str(h) for h in (f.heading_path or [])),
                )
                for f in rows
            ]
            pages = [
                (p.page_no, p.char_start, p.char_end)
                for p in (
                    await db.execute(select(DocumentPage).where(DocumentPage.version_id == self.version_id))
                ).scalars()
            ]
            if not fragments:
                raise AnalysisFailed("version_has_no_text")
            self.index = FragmentIndex(fragments, pages)
            self.chunks = [
                _Chunk(
                    c.id,
                    c.chunk_index,
                    c.fragment_seq_start,
                    c.fragment_seq_end,
                    c.status,
                    c.error_code,
                    c.result,
                )
                for c in (
                    await db.execute(
                        select(AnalysisChunk)
                        .where(AnalysisChunk.analysis_id == self.analysis_id)
                        .order_by(AnalysisChunk.chunk_index)
                    )
                ).scalars()
            ]
            self.target = await self._build_target(db, self.snapshot)
            verifier = self.snapshot.get("verifier") or {}
            if verifier.get("same_as_analysis", True):
                self.verifier: Target = self.target
            else:
                self.verifier = await self._build_target(db, verifier)
            await db.commit()  # закрываем транзакцию чтения до HTTP-запросов
        self.verifier_unavailable: str | None = None
        chunking = self.snapshot.get("chunking", {})
        self.text_budget_chars = int(chunking.get("budget_tokens", 4000) * CHARS_PER_TOKEN)
        self.token_limit = int(
            self.snapshot.get("limits", {}).get("max_analysis_tokens", self.settings.analysis_max_tokens)
        )

    async def _build_target(self, db: AsyncSession, snap: dict[str, Any]) -> Target:
        provider_snap, model_snap = snap["provider"], snap["model"]
        row = (
            await db.execute(
                select(ModelConfig, ProviderConfig)
                .join(ProviderConfig, ProviderConfig.id == ModelConfig.provider_config_id)
                .where(ModelConfig.id == uuid.UUID(model_snap["id"]))
            )
        ).one_or_none()
        if row is None:
            raise AnalysisFailed("model_not_found")
        model, provider = row
        if not model.enabled or not provider.enabled:
            raise AnalysisFailed("model_disabled")
        if provider.is_cloud:
            if not await cloud_models_enabled(db):
                raise AnalysisFailed("cloud_models_disabled")
            if not self.cloud_transfer:
                raise AnalysisFailed("cloud_consent_missing")
        try:
            llm = self.llm_factory.build(provider)
        except ProviderError as exc:
            raise AnalysisFailed(exc.code) from None
        return Target(
            llm=llm,
            provider_id=provider.id,
            provider_type=ProviderType(provider_snap["type"]),
            is_cloud=provider.is_cloud,
            external_model_id=model_snap["external_model_id"],
            context_window=model_snap.get("context_window"),
            max_output_tokens=int(
                model_snap.get("max_output_tokens") or self.settings.analysis_default_max_output_tokens
            ),
            params=dict(model_snap.get("generation_params") or {}),
            structured_output=StructuredOutputSupport(model_snap.get("structured_output", "unknown")),
            snapshot={
                "model_id": model_snap["id"],
                "display_name": model_snap.get("display_name"),
                "external_model_id": model_snap["external_model_id"],
                "provider_type": provider_snap["type"],
                "prompt_version": prompts.PROMPT_VERSIONS["verifier"],
            },
        )

    # ================================================================ вызов модели

    def _tokens_used(self) -> int:
        return int(self.usage.get("prompt_tokens", 0)) + int(self.usage.get("completion_tokens", 0))

    def _budget_exhausted(self) -> bool:
        return self._tokens_used() >= self.token_limit

    def _add_usage(self, result: StructuredResult) -> tuple[int | None, int | None]:
        usage = dict(self.usage)
        usage["calls"] = int(usage.get("calls", 0)) + 1
        u = result.usage
        prompt = u.prompt_tokens if u else None
        completion = u.completion_tokens if u else None
        if prompt is None and completion is None:
            usage["calls_without_usage"] = int(usage.get("calls_without_usage", 0)) + 1
        usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + (prompt or 0)
        usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + (completion or 0)
        if u is not None and u.cost is not None:
            usage["cost"] = str(Decimal(str(usage.get("cost", "0"))) + u.cost)
            usage["cost_currency"] = u.cost_currency
        self.usage = usage
        return prompt, completion

    async def _cancel_requested(self) -> bool:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(Analysis.cancel_requested_at, Analysis.progress).where(
                        Analysis.id == self.analysis_id
                    )
                )
            ).one_or_none()
            await db.commit()
        if row is None or (row[1] or {}).get("run_id") != self.run_id:
            raise Superseded()
        return row[0] is not None

    async def _preflight(self, target: Target) -> None:
        if await self._cancel_requested():
            raise AnalysisCancelled()
        if not target.is_cloud:
            return
        async with self.factory() as db:
            enabled = (
                await db.execute(
                    select(ProviderConfig.enabled).where(ProviderConfig.id == target.provider_id)
                )
            ).scalar_one_or_none()
            cloud = await cloud_models_enabled(db)
            await db.commit()
        if not enabled:
            raise AnalysisFailed("model_disabled")
        if not cloud:
            raise AnalysisFailed("cloud_models_disabled")

    def _params(self, target: Target, messages: Sequence[ChatMessage]) -> GenerationParams:
        params = GenerationParams.from_mapping(target.params, max_output_tokens=target.max_output_tokens)
        if target.provider_type is ProviderType.ollama and params.num_ctx is None:
            # Ollama по умолчанию берёт маленький контекст и молча обрезает запрос — задаём явно,
            # но не больше, чем нужно (большой контекст на CPU резко замедляет генерацию).
            needed = estimate_tokens(sum(len(m.content) for m in messages)) + target.max_output_tokens + 1024
            limit = target.context_window or DEFAULT_CONTEXT_TOKENS
            num_ctx = min(limit, math.ceil(needed / 2048) * 2048)
            params = replace(params, num_ctx=num_ctx)
        return params

    async def _call(self, target: Target, req: StructuredRequest) -> StructuredResult:
        await self._preflight(target)
        task = asyncio.create_task(target.llm.generate_structured(req))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.settings.analysis_cancel_poll_seconds)
                if done:
                    break
                if await self._cancel_requested():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ProviderError):
                        await task
                    raise AnalysisCancelled()
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    async def _generate(
        self,
        target: Target,
        model_cls: type[T],
        messages: list[ChatMessage],
        schema_name: str,
        *,
        role: str = "analysis",
    ) -> Generated[T]:
        """Вызов модели с валидацией по схеме и одной попыткой исправления формата (repair)."""
        schema = provider_schema(model_cls)
        attempt_messages = messages
        prompt_total: int | None = None
        completion_total: int | None = None
        for attempt in (0, 1):
            req = StructuredRequest(
                model=target.external_model_id,
                messages=attempt_messages,
                json_schema=schema,
                schema_name=schema_name,
                params=self._params(target, attempt_messages),
                structured_output=target.structured_output,
            )
            try:
                result = await self._call(target, req)
            except InvalidModelOutput as exc:
                if exc.code == "output_truncated":
                    raise OutputError("output_truncated") from None
                if attempt == 1:
                    raise OutputError("invalid_model_output") from None
                attempt_messages = [*messages, prompts.repair_message([exc.code])]
                continue
            except ContextTooLong:
                raise OutputError("context_too_long") from None
            except ProviderError as exc:
                raise self._provider_failure(exc, role) from None
            prompt, completion = self._add_usage(result)
            if prompt is not None:
                prompt_total = (prompt_total or 0) + prompt
            if completion is not None:
                completion_total = (completion_total or 0) + completion
            try:
                value = model_cls.model_validate(result.parsed_json)
            except ValidationError as exc:
                log.info(
                    "analysis_output_invalid", schema=schema_name, attempt=attempt, errors=len(exc.errors())
                )
                if attempt == 1:
                    raise OutputError("invalid_model_output") from None
                attempt_messages = [*messages, prompts.repair_message(_validation_errors(exc))]
                continue
            return Generated(value, prompt_total, completion_total)
        raise OutputError("invalid_model_output")  # pragma: no cover - цикл всегда завершается выше

    def _provider_failure(self, exc: ProviderError, role: str) -> Exception:
        if exc.retryable and not self.last_attempt:
            return RetryLater(exc.code)
        if role == "verifier":
            return OutputError(exc.code, fatal=True)
        return AnalysisFailed(exc.code)

    # ================================================================ этапы

    async def _scan_injection(self) -> None:
        if "injection" in self.stage_results:
            return
        hits = scan((f.key, f.text) for f in self.index.fragments)
        self.stage_results["injection"] = [{"fragment_key": h.fragment_key, "code": h.code} for h in hits]
        async with self._tx() as (_db, analysis):
            self._set_progress(analysis, stage="structure")
        if hits:
            log.warning("analysis_prompt_injection_suspected", hits=len(hits))

    async def _structure(self) -> None:
        if "structure" in self.stage_results:
            return
        fragments = self.index.fragments
        sections = build_sections(fragments)
        structure: dict[str, Any] = {
            "sections": sections,
            "contract_type": None,
            "subject": None,
            "parties": [],
            "source": "deterministic",
        }
        if self.depth is not AnalysisDepth.quick and not self._budget_exhausted():
            heads = outline(fragments, sections)
            edges = edge_fragments(fragments)
            messages = prompts.structure_messages(
                outline=[self._pf(f) for f in heads], text=[self._pf(f) for f in edges]
            )
            try:
                out = await self._generate(self.target, StructureOut, messages, "contract_structure")
                structure = merge_llm_structure(structure, out.value, fragments)
                structure["source"] = "llm+deterministic"
            except OutputError as exc:
                structure["llm_error"] = exc.code
                log.info("analysis_structure_llm_failed", code=exc.code)
        self.stage_results["structure"] = structure
        async with self._tx() as (_db, analysis):
            self._set_progress(
                analysis, stage="chunks", chunks_total=len(self.chunks), chunks_done=self._done_count()
            )

    def _pf(self, f: FragmentRef) -> prompts.PromptFragment:
        return prompts.PromptFragment(key=f.key, text=f.text, clause_ref=f.clause_ref, kind=f.kind)

    def _done_count(self) -> int:
        return sum(1 for c in self.chunks if c.status is ChunkStatus.done)

    async def _chunks(self) -> None:
        structure = self.stage_results.get("structure")
        total = len(self.chunks)
        for pos, chunk in enumerate(self.chunks):
            if chunk.status in (ChunkStatus.done, ChunkStatus.failed, ChunkStatus.skipped):
                continue
            if self._budget_exhausted():
                await self._save_chunk(chunk, ChunkStatus.skipped, error_code="token_budget_exhausted")
                continue
            previous_end = self.chunks[pos - 1].seq_end if pos > 0 else -1
            fragments = [
                self.index.by_seq[s]
                for s in range(chunk.seq_start, chunk.seq_end + 1)
                if s in self.index.by_seq
            ]
            context = frozenset(f.key for f in fragments if f.seq <= previous_end)
            messages = prompts.chunk_messages(
                fragments=[self._pf(f) for f in fragments],
                context_keys=context,
                party=self.party,
                party_name=self.party_name,
                categories=self.categories,
                depth=self.depth,
                structure=structure,
                part=chunk.index + 1,
                parts=total,
            )
            async with self._tx() as (db, _analysis):
                await db.execute(
                    update(AnalysisChunk)
                    .where(AnalysisChunk.id == chunk.id)
                    .values(status=ChunkStatus.running, attempts=AnalysisChunk.attempts + 1)
                )
            try:
                out = await self._generate(self.target, RiskList, messages, "chunk_risks")
            except OutputError as exc:
                await self._save_chunk(chunk, ChunkStatus.failed, error_code=exc.code)
                log.info("analysis_chunk_failed", chunk=chunk.index, code=exc.code)
                continue
            chunk.result = out.value.model_dump(mode="json")
            await self._save_chunk(
                chunk, ChunkStatus.done, tokens_in=out.prompt_tokens, tokens_out=out.completion_tokens
            )
            log.info("analysis_chunk_done", chunk=chunk.index, risks=len(out.value.risks))
        if not any(c.status is ChunkStatus.done for c in self.chunks):
            raise AnalysisFailed("analysis_no_chunks_succeeded")

    async def _save_chunk(
        self,
        chunk: _Chunk,
        status: ChunkStatus,
        *,
        error_code: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        chunk.status = status
        chunk.error_code = error_code
        async with self._tx() as (db, analysis):
            await db.execute(
                update(AnalysisChunk)
                .where(AnalysisChunk.id == chunk.id)
                .values(
                    status=status,
                    error_code=error_code,
                    result=chunk.result if status is ChunkStatus.done else None,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            )
            self._set_progress(
                analysis, stage="chunks", chunks_total=len(self.chunks), chunks_done=self._done_count()
            )

    async def _cross_section(self) -> None:
        if "cross_section" in self.stage_results:
            return
        find_contradictions = RiskCategory.contradiction in self.categories
        find_missing = RiskCategory.missing_clause in self.categories
        result: dict[str, Any]
        if not (find_contradictions or find_missing):
            result = {"status": "skipped", "reason": "not_requested"}
        elif self._budget_exhausted():
            result = {"status": "skipped", "reason": "token_budget_exhausted"}
        else:
            structure = self.stage_results.get("structure") or {}
            heads = outline(self.index.fragments, structure.get("sections", []))
            used = sum(len(f.text) + 2 for f in heads)
            keys = key_fragments(
                self.index.fragments, max(2000, self.text_budget_chars - used), exclude={f.seq for f in heads}
            )
            cards = []
            for chunk in self.chunks:
                if chunk.status is not ChunkStatus.done or not chunk.result:
                    continue
                for risk in chunk.result.get("risks", []):
                    cards.append(
                        {
                            "title": risk["title"],
                            "category": risk["category"],
                            "kind": risk["kind"],
                            "fragments": sorted({e["fragment_id"] for e in risk.get("evidence", [])}),
                        }
                    )
            messages = prompts.cross_section_messages(
                risk_cards=cards[:MAX_RISK_CARDS],
                outline=[self._pf(f) for f in heads],
                key_fragments=[self._pf(f) for f in keys],
                party=self.party,
                party_name=self.party_name,
                categories=self.categories,
                structure=structure,
                find_contradictions=find_contradictions,
                find_missing=find_missing,
            )
            scope = SeqScope.of([f.seq for f in heads] + [f.seq for f in keys])
            try:
                out = await self._generate(self.target, RiskList, messages, "cross_section_risks")
                result = {
                    "status": "done",
                    "result": out.value.model_dump(mode="json"),
                    "scope": scope.to_json(),
                    "outline_fragments": len(heads),
                    "key_fragments": len(keys),
                }
            except OutputError as exc:
                result = {"status": "failed", "code": exc.code}
                log.info("analysis_cross_section_failed", code=exc.code)
        self.stage_results["cross_section"] = result
        async with self._tx() as (_db, analysis):
            self._set_progress(analysis, stage="evidence")

    # ---------------------------------------------------------------- риски и доказательства

    def _in_scope(self, risk: RiskItem) -> bool:
        if risk.category in self.categories:
            return True
        if risk.kind is RiskKind.missing_clause and RiskCategory.missing_clause in self.categories:
            return True
        return risk.kind is RiskKind.contradiction and RiskCategory.contradiction in self.categories

    def _candidates(self) -> tuple[list[Candidate], int]:
        candidates: list[Candidate] = []
        dropped = 0
        for chunk in self.chunks:
            if chunk.status is not ChunkStatus.done or not chunk.result:
                continue
            seqs = set(range(chunk.seq_start, chunk.seq_end + 1))
            for risk in RiskList.model_validate(chunk.result).risks:
                if self._in_scope(risk):
                    candidates.append(Candidate(risk, {str(chunk.id)}, seqs))
                else:
                    dropped += 1
        cross = self.stage_results.get("cross_section") or {}
        if cross.get("status") == "done":
            seqs = set(SeqScope.from_json(cross["scope"]).seqs())
            for risk in RiskList.model_validate(cross["result"]).risks:
                if self._in_scope(risk):
                    candidates.append(Candidate(risk, {"cross_section"}, seqs))
                else:
                    dropped += 1
        return candidates, dropped

    def _sections_for(self, seqs: set[int]) -> list[str]:
        structure = self.stage_results.get("structure") or {}
        return [
            s["title"]
            for s in structure.get("sections", [])
            if any(s["start_seq"] <= q <= s["end_seq"] for q in seqs)
        ]

    async def _materialize(self) -> None:
        if self.stage_results.get("materialized"):
            return
        candidates, dropped = self._candidates()
        merged = deduplicate(candidates)

        def order(m: Any) -> tuple[int, int, str]:
            seqs = [
                self.index.by_key[e.fragment_id].seq for e in m.evidence if e.fragment_id in self.index.by_key
            ]
            return (-SEVERITY_RANK[m.risk.severity], min(seqs) if seqs else 10**9, m.risk.title)

        merged.sort(key=order)
        scopes: dict[str, list[list[int]]] = {}
        async with self._tx() as (db, analysis):
            for number, m in enumerate(merged, start=1):
                risk = m.risk
                key = f"R-{number:03d}"
                scope = SeqScope.of(m.scope_seqs)
                scopes[key] = scope.to_json()
                notes: dict[str, Any] = {}
                if m.merged_count > 1:
                    notes["merged_from"] = m.merged_count
                if "cross_section" in m.sources:
                    notes["from_cross_section"] = True
                texts = " ".join(
                    t
                    for t in (
                        risk.description,
                        risk.reason,
                        risk.recommendation,
                        risk.suggested_revision or "",
                    )
                    if t
                )
                if _LEGAL_REFERENCE.search(texts):
                    notes["legal_reference_unverified"] = True
                if self.party is AnalysisParty.unknown and _ADDRESSES_READER.search(
                    texts + " " + risk.consequences
                ):
                    notes["party_wording_not_neutral"] = True
                search_scope = None
                if risk.kind is RiskKind.missing_clause:
                    search_scope = {
                        "model_scope": risk.search_scope,
                        "checked_sections": self._sections_for(m.scope_seqs)[:100],
                        "checked_fragments": len(m.scope_seqs),
                        "limitations": [
                            "Отсутствие условия установлено моделью по просмотренному тексту; сервер "
                            "не может доказать отсутствие — проверьте договор, включая приложения.",
                        ]
                        + (
                            ["Межраздельный проход видел заголовки и ключевые фрагменты, а не весь текст."]
                            if "cross_section" in m.sources
                            else []
                        ),
                    }
                row = AnalysisRisk(
                    id=uuid.uuid4(),
                    analysis_id=self.analysis_id,
                    risk_key=key,
                    kind=risk.kind,
                    title=risk.title,
                    category=risk.category.value,
                    severity=risk.severity,
                    description=risk.description,
                    reason=risk.reason,
                    consequences=risk.consequences,
                    affected_party=risk.affected_party,
                    recommendation=risk.recommendation,
                    suggested_revision=risk.suggested_revision,
                    model_confidence=risk.model_confidence,
                    validation_status=ValidationStatus.pending,
                    validation_notes=notes,
                    search_scope=search_scope,
                    source_chunk_ids=sorted(uuid.UUID(s) for s in m.sources if s != "cross_section"),
                    dedup_group=f"G-{number:03d}" if m.merged_count > 1 else None,
                )
                db.add(row)
                for item in m.evidence:
                    db.add(self._evidence_row(row.id, item.fragment_id, item.quote, scope, attempt=0))
            self.stage_results["materialized"] = True
            self.stage_results["risk_scopes"] = scopes
            self.stage_results["dedup"] = {
                "candidates": len(candidates),
                "risks": len(merged),
                "dropped_out_of_scope": dropped,
            }
            analysis.status = AnalysisStatus.verifying
            self._set_progress(analysis, stage="evidence", risks=len(merged))
        log.info(
            "analysis_risks_materialized", candidates=len(candidates), risks=len(merged), dropped=dropped
        )

    def _evidence_row(
        self, risk_id: uuid.UUID, fragment_key: str, quote: str, scope: SeqScope, *, attempt: int
    ) -> RiskEvidence:
        match: EvidenceMatch = match_evidence(self.index, scope, fragment_key, quote)
        return RiskEvidence(
            id=uuid.uuid4(),
            risk_id=risk_id,
            fragment_id=match.fragment.id if match.fragment else None,
            claimed_fragment_key=fragment_key[:32],
            fragment_ids_span=[f.id for f in match.span],
            quote=quote,
            quote_normalized=match.quote_normalized,
            match_status=match.status,
            match_char_start=match.start,
            match_char_end=match.end,
            page_start=match.page_start,
            page_end=match.page_end,
            clause_ref=match.clause_ref,
            relevance_status=RelevanceStatus.unchecked,
            relevance_rationale=None,
            relevance_checked_by=None,
            verifier_model_snapshot={"match_notes": match.notes} if match.notes else None,
            attempt=attempt,
        )

    async def _load_risks(self) -> list[tuple[AnalysisRisk, list[RiskEvidence]]]:
        async with self.factory() as db:
            risks = list(
                (
                    await db.execute(
                        select(AnalysisRisk)
                        .where(AnalysisRisk.analysis_id == self.analysis_id)
                        .order_by(AnalysisRisk.risk_key)
                    )
                ).scalars()
            )
            evidence = list(
                (
                    await db.execute(
                        select(RiskEvidence)
                        .join(AnalysisRisk, AnalysisRisk.id == RiskEvidence.risk_id)
                        .where(AnalysisRisk.analysis_id == self.analysis_id)
                        .order_by(RiskEvidence.created_at, RiskEvidence.id)
                    )
                ).scalars()
            )
            await db.commit()
        by_risk: dict[uuid.UUID, list[RiskEvidence]] = {r.id: [] for r in risks}
        for e in evidence:
            by_risk[e.risk_id].append(e)
        return [(r, by_risk[r.id]) for r in risks]

    @staticmethod
    def _current(evidence: Sequence[RiskEvidence]) -> list[RiskEvidence]:
        """Актуальные доказательства: последней попытки (повтор цитаты заменяет исходные)."""
        if not evidence:
            return []
        last = max(e.attempt for e in evidence)
        return [e for e in evidence if e.attempt == last]

    async def _update_notes(self, risk_id: uuid.UUID, **values: Any) -> None:
        async with self._tx() as (db, _analysis):
            risk = (await db.execute(select(AnalysisRisk).where(AnalysisRisk.id == risk_id))).scalar_one()
            notes = dict(risk.validation_notes or {})
            notes.update(values)
            risk.validation_notes = notes

    async def _quote_retries(self) -> None:
        scopes = self.stage_results.get("risk_scopes", {})
        for risk, evidence in await self._load_risks():
            notes = risk.validation_notes or {}
            if risk.kind is RiskKind.missing_clause or "quote_retry" in notes:
                continue
            current = self._current(evidence)
            problems = sorted(
                {
                    f"{e.claimed_fragment_key}: {e.match_status.value}"
                    for e in current
                    if e.match_status not in MATCHED
                }
            )
            if current and not problems:
                continue
            if not current:
                problems = ["доказательства не приведены"]
            used = int(self.stage_results.get("quote_retries_used", 0))
            if used >= self.settings.analysis_quote_retry_limit:
                await self._update_notes(risk.id, quote_retry="skipped_limit")
                continue
            if self._budget_exhausted():
                await self._update_notes(risk.id, quote_retry="skipped_budget")
                continue
            scope = SeqScope.from_json(scopes.get(risk.risk_key, []))
            claimed = [
                self.index.by_key[e.claimed_fragment_key].seq
                for e in current
                if e.claimed_fragment_key in self.index.by_key
            ]
            fragments = self._context_fragments(scope, claimed)
            messages = prompts.quote_retry_messages(
                risk={
                    "title": risk.title,
                    "category": risk.category,
                    "kind": risk.kind.value,
                    "description": risk.description,
                    "reason": risk.reason,
                },
                problems=[
                    p.replace("not_found", "цитата не найдена в тексте фрагмента")
                    .replace("fragment_mismatch", "фрагмент не существует или не был в запросе")
                    .replace("too_short", "цитата слишком короткая")
                    for p in problems
                ],
                fragments=[self._pf(f) for f in fragments],
            )
            self.stage_results["quote_retries_used"] = used + 1
            try:
                out = await self._generate(self.target, QuoteRetryOut, messages, "quote_retry")
            except OutputError as exc:
                await self._update_notes(risk.id, quote_retry="failed", quote_retry_code=exc.code)
                continue
            async with self._tx() as (db, _analysis):
                row = (await db.execute(select(AnalysisRisk).where(AnalysisRisk.id == risk.id))).scalar_one()
                notes = dict(row.validation_notes or {})
                if out.value.withdraw:
                    notes["quote_retry"] = "withdrawn"
                elif not out.value.evidence:
                    notes["quote_retry"] = "no_evidence"
                else:
                    notes["quote_retry"] = "quoted"
                    for item in out.value.evidence:
                        db.add(self._evidence_row(risk.id, item.fragment_id, item.quote, scope, attempt=1))
                row.validation_notes = notes
            log.info("analysis_quote_retry", risk=risk.risk_key, outcome=notes["quote_retry"])

    def _context_fragments(self, scope: SeqScope, claimed: Sequence[int]) -> list[FragmentRef]:
        seqs = [s for s in scope.seqs() if s in self.index.by_seq]
        if claimed:
            seqs.sort(key=lambda s: min(abs(s - c) for c in claimed))
        picked: list[int] = []
        used = 0
        for s in seqs:
            size = len(self.index.by_seq[s].text) + 2
            if used + size > self.text_budget_chars and picked:
                break
            picked.append(s)
            used += size
        return [self.index.by_seq[s] for s in sorted(picked)]

    def _span_text(self, e: RiskEvidence) -> tuple[str, str, list[str]]:
        """Цитата — дословный текст документа по смещениям совпадения; контекст — фрагменты ±1."""
        span_ids = set(e.fragment_ids_span)
        span = [f for f in self.index.fragments if f.id in span_ids]
        if not span or e.match_char_start is None or e.match_char_end is None:
            return e.quote, e.quote, []
        combined = SEPARATOR.join(f.text for f in span)
        base = span[0].char_start
        quote = combined[e.match_char_start - base : e.match_char_end - base]
        neighbours = [
            self.index.by_seq.get(span[0].seq - 1),
            *span,
            self.index.by_seq.get(span[-1].seq + 1),
        ]
        context = SEPARATOR.join(f.text for f in neighbours if f is not None)
        if len(context) > VERIFIER_CONTEXT_CHARS * 2:
            at = max(0, context.find(quote))
            context = context[max(0, at - VERIFIER_CONTEXT_CHARS) : at + len(quote) + VERIFIER_CONTEXT_CHARS]
        return quote, context, [f.kind for f in span]

    async def _verify(self) -> None:
        for risk, evidence in await self._load_risks():
            notes = risk.validation_notes or {}
            if "verifier" in notes or notes.get("quote_retry") == "withdrawn":
                continue
            matched = [e for e in self._current(evidence) if e.match_status in MATCHED]
            if not matched:
                await self._update_notes(risk.id, verifier="no_matched_quotes")
                continue
            if self.verifier_unavailable:
                await self._update_notes(
                    risk.id, verifier="unavailable", verifier_code=self.verifier_unavailable
                )
                continue
            if self._budget_exhausted():
                await self._update_notes(risk.id, verifier="skipped_budget")
                continue
            items = []
            kinds: dict[int, list[str]] = {}
            for number, e in enumerate(matched, start=1):
                quote, context, span_kinds = self._span_text(e)
                items.append(prompts.VerifierEvidence(number, quote, context))
                kinds[number] = span_kinds
            messages = prompts.verifier_messages(
                risk={
                    "title": risk.title,
                    "category": risk.category,
                    "kind": risk.kind.value,
                    "reason": risk.reason,
                },
                evidence=items,
            )
            try:
                out = await self._generate(
                    self.verifier, VerifierOut, messages, "evidence_relevance", role="verifier"
                )
            except OutputError as exc:
                if exc.fatal:
                    self.verifier_unavailable = exc.code
                    await self._update_notes(risk.id, verifier="unavailable", verifier_code=exc.code)
                else:
                    await self._update_notes(risk.id, verifier="failed", verifier_code=exc.code)
                continue
            await self._save_verdicts(risk.id, matched, out.value, kinds)

    async def _save_verdicts(
        self,
        risk_id: uuid.UUID,
        matched: Sequence[RiskEvidence],
        out: VerifierOut,
        kinds: dict[int, list[str]],
    ) -> None:
        verdicts = {v.evidence_no: v for v in out.verdicts}
        async with self._tx() as (db, _analysis):
            complete = True
            rules_applied = False
            for number, e in enumerate(matched, start=1):
                row = (await db.execute(select(RiskEvidence).where(RiskEvidence.id == e.id))).scalar_one()
                verdict = verdicts.get(number)
                snapshot = dict(row.verifier_model_snapshot or {})
                snapshot.update(self.verifier.snapshot)
                row.verifier_model_snapshot = snapshot
                if verdict is None:
                    complete = False
                    row.relevance_status = RelevanceStatus.unchecked
                    row.relevance_checked_by = "verifier_llm"
                    continue
                status, capped = apply_relevance_rules(verdict.status, kinds[number])
                rules_applied = rules_applied or capped
                row.relevance_status = status
                row.relevance_rationale = verdict.rationale
                row.relevance_checked_by = "verifier_llm+rules" if capped else "verifier_llm"
            risk_row = (await db.execute(select(AnalysisRisk).where(AnalysisRisk.id == risk_id))).scalar_one()
            notes = dict(risk_row.validation_notes or {})
            notes["verifier"] = "done" if complete else "incomplete"
            if rules_applied:
                notes["heading_quote_capped"] = True
            risk_row.validation_notes = notes

    async def _finalize(self) -> None:
        loaded = await self._load_risks()
        infos: list[RiskInfo] = []
        async with self._tx() as (db, analysis):
            for risk, evidence in loaded:
                notes = dict(risk.validation_notes or {})
                states = [
                    EvidenceState(
                        e.match_status,
                        e.relevance_status,
                        self.index.by_key[e.claimed_fragment_key].seq
                        if e.claimed_fragment_key in self.index.by_key
                        else None,
                    )
                    for e in self._current(evidence)
                ]
                status, reasons = final_status(
                    risk.kind, states, withdrawn=notes.get("quote_retry") == "withdrawn"
                )
                notes["status_reasons"] = reasons
                row = (await db.execute(select(AnalysisRisk).where(AnalysisRisk.id == risk.id))).scalar_one()
                row.validation_status = status
                row.validation_notes = notes
                infos.append(
                    RiskInfo(
                        risk.kind, risk.severity, status, notes.get("verifier"), notes.get("quote_retry")
                    )
                )
            structure = self.stage_results.get("structure") or {}
            coverage, warnings = build_coverage(
                fragment_keys={f.seq: f.key for f in self.index.fragments},
                chunks=[
                    ChunkInfo(c.index, c.seq_start, c.seq_end, c.status, c.error_code) for c in self.chunks
                ],
                sections=structure.get("sections", []),
                cross_section=self.stage_results.get("cross_section") or {},
                risks=infos,
                page_source=self.page_source,
            )
            names = prompts.party_names(structure)
            label = (
                names.get(self.party.value)
                if self.party in (AnalysisParty.party_a, AnalysisParty.party_b)
                else None
            )
            analysis.coverage = coverage
            analysis.summary = build_summary(
                party=self.party,
                party_name=self.party_name,
                party_label=label,
                snapshot=self.snapshot,
                categories=[c.value for c in self.categories],
                depth=self.depth.value,
                risks=infos,
                warnings=warnings,
                injection_hits=self.stage_results.get("injection", []),
                structure=structure,
                page_source=self.page_source,
            )
            analysis.status = AnalysisStatus.completed_with_warnings if warnings else AnalysisStatus.completed
            analysis.finished_at = _now()
            analysis.error_code = None
            analysis.error_message_safe = None
            self.stage_results["finalized"] = True
            self._set_progress(analysis, stage="done")
        # Ключ без слова «token»: такие ключи маскируются фильтром логов как секреты.
        log.info("analysis_completed", risks=len(infos), warnings=len(warnings), usage=self._tokens_used())
