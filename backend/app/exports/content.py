"""Содержимое отчёта: данные анализа, документа и рисков в виде, общем для всех форматов.

Из snapshot конфигурации берутся только перечисленные поля (название и ID модели, провайдер, версии
промптов) — ключи, адреса и прочие настройки в отчёт не попадают.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.prompts import CATEGORY_LABELS
from app.models.analyses import Analysis, AnalysisRisk, RiskEvidence
from app.models.documents import Document, DocumentFragment, DocumentVersion
from app.models.enums import AnalysisParty, RiskCategory, ValidationStatus
from app.redaction.categories import redaction_line

SEVERITY = {
    "critical": "Критический",
    "high": "Высокий",
    "medium": "Средний",
    "low": "Низкий",
    "informational": "Информационный",
}
VALIDATION = {
    "verified_match": "Подтверждён: цитата найдена в тексте и подтверждает риск по оценке верификатора",
    "needs_review": "Требует проверки",
    "rejected": "Не подтверждён (отклонён)",
    "pending": "Не проверен",
}
VALIDATION_SHORT = {
    "verified_match": "подтверждён",
    "needs_review": "требует проверки",
    "rejected": "отклонён",
    "pending": "не проверен",
}
KIND = {
    "clause_risk": "Риск в условии",
    "contradiction": "Противоречие",
    "missing_clause": "Отсутствующее условие",
}
AFFECTED = {
    "party_a": "Сторона A",
    "party_b": "Сторона B",
    "both": "Обе стороны",
    "unclear": "Не определено",
}
PARTY = {
    "party_a": "Сторона A (первая в преамбуле)",
    "party_b": "Сторона B (вторая в преамбуле)",
    "named": "Указанная сторона",
    "unknown": "Не указана (нейтральный анализ)",
}
DEPTH = {"quick": "Быстрый", "standard": "Стандартный", "deep": "Углублённый"}
MATCH = {
    "exact": "цитата совпадает дословно",
    "normalized": "цитата совпадает (с учётом пробелов, переносов, кавычек и тире)",
    "not_found": "цитата не найдена в тексте",
    "fragment_mismatch": "фрагмент не существует или не был передан модели",
    "too_short": "цитата слишком короткая, не доказывает риск",
    "pending": "не проверена",
}
RELEVANCE = {
    "supports": "подтверждает риск",
    "partially_supports": "подтверждает частично — требует проверки",
    "does_not_support": "не подтверждает риск",
    "unchecked": "смысловая связь не проверена",
}
STATUS = {
    "completed": "Завершён",
    "completed_with_warnings": "Завершён с предупреждениями",
}


def category_label(code: str) -> str:
    try:
        return CATEGORY_LABELS[RiskCategory(code)]
    except ValueError:
        return code


@dataclass
class ReportEvidence:
    fragment_key: str
    quote: str
    pages: str | None
    clause_ref: str | None
    match: str
    relevance: str
    rationale: str | None


@dataclass
class ReportRisk:
    key: str
    kind: str
    title: str
    category: str
    severity: str
    validation_status: str
    validation: str
    affected_party: str
    description: str
    reason: str
    consequences: str
    recommendation: str
    suggested_revision: str | None
    model_confidence: float | None
    evidence: list[ReportEvidence] = field(default_factory=list)
    search_scope: dict[str, Any] | None = None
    legal_reference: bool = False


@dataclass
class Report:
    analysis_id: uuid.UUID
    generated_at: datetime
    document_title: str
    version_no: int
    page_note: str | None
    analysis_status: str
    created_at: datetime
    finished_at: datetime | None
    party: str
    party_name: str | None
    depth: str
    categories: list[str]
    model: list[tuple[str, str]]
    statement: str
    counts: list[tuple[str, str]]
    coverage: list[str]
    limitations: list[str]
    disclaimer: str
    risks: list[ReportRisk]
    rejected: list[ReportRisk]

    @property
    def all_risks(self) -> list[ReportRisk]:
        return [*self.risks, *self.rejected]


def _pages(start: int | None, end: int | None) -> str | None:
    if start is None:
        return None
    return str(start) if end in (None, start) else f"{start}–{end}"


def _model_lines(snapshot: dict[str, Any]) -> list[tuple[str, str]]:
    model = snapshot.get("model") or {}
    provider = snapshot.get("provider") or {}
    verifier = snapshot.get("verifier") or {}
    pipeline = snapshot.get("pipeline") or {}
    lines = [
        ("Модель", f"{model.get('display_name', '—')} ({model.get('external_model_id', '—')})"),
        (
            "Провайдер",
            f"{provider.get('name', '—')} · {'облачный' if provider.get('is_cloud') else 'локальный'}",
        ),
    ]
    if verifier.get("same_as_analysis", True):
        lines.append(("Верификатор цитат", "та же модель"))
    else:
        vm = verifier.get("model") or {}
        lines.append(
            ("Верификатор цитат", f"{vm.get('display_name', '—')} ({vm.get('external_model_id', '—')})")
        )
    redaction = redaction_line(snapshot.get("redaction"))
    if redaction:
        lines.append(("Маскировка данных", redaction))
    elif "redaction" in snapshot and not provider.get("is_cloud"):
        lines.append(
            ("Маскировка данных", "не применялась: локальная модель, текст не покидает инфраструктуру")
        )
    lines.append(("Версия модели в каталоге", str(model.get("version", "—"))))
    prompts = pipeline.get("prompts") or {}
    versions = [pipeline.get("pipeline"), pipeline.get("schema"), *prompts.values()]
    lines.append(("Версии конвейера и промптов", ", ".join(str(v) for v in versions if v)))
    captured = snapshot.get("captured_at")
    if captured:
        lines.append(("Snapshot конфигурации", str(captured)))
    return lines


def _coverage_lines(coverage: dict[str, Any] | None) -> list[str]:
    if not coverage:
        return []
    lines = [
        f"Проанализировано фрагментов: {coverage.get('fragments_analyzed', 0)} из "
        f"{coverage.get('fragments_total', 0)}; частей текста: {coverage.get('chunks_done', 0)} из "
        f"{coverage.get('chunks_total', 0)}.",
    ]
    sections = coverage.get("sections") or []
    missed = [s for s in sections if s.get("status") != "analyzed"]
    if sections:
        lines.append(f"Разделов: {len(sections)}; проанализированы не полностью: {len(missed)}.")
    for s in missed[:30]:
        state = "частично" if s.get("status") == "partial" else "не проанализирован"
        lines.append(f"— {s.get('title', '')} ({state})")
    cross = (coverage.get("cross_section") or {}).get("status")
    if cross:
        names = {"done": "выполнен", "failed": "не выполнен (ошибка)", "skipped": "не выполнялся"}
        lines.append(f"Поиск межраздельных противоречий и отсутствующих условий: {names.get(cross, cross)}.")
    return lines


async def build_report(db: AsyncSession, analysis_id: uuid.UUID) -> Report:
    analysis = (await db.execute(select(Analysis).where(Analysis.id == analysis_id))).scalar_one()
    document = (await db.execute(select(Document).where(Document.id == analysis.document_id))).scalar_one()
    version = (
        await db.execute(select(DocumentVersion).where(DocumentVersion.id == analysis.version_id))
    ).scalar_one()
    risks = list(
        (
            await db.execute(
                select(AnalysisRisk)
                .where(AnalysisRisk.analysis_id == analysis.id)
                .order_by(AnalysisRisk.risk_key)
            )
        ).scalars()
    )
    evidence_rows = (
        list(
            (
                await db.execute(
                    select(RiskEvidence, DocumentFragment.fragment_key)
                    .outerjoin(DocumentFragment, DocumentFragment.id == RiskEvidence.fragment_id)
                    .where(RiskEvidence.risk_id.in_([r.id for r in risks]))
                    .order_by(RiskEvidence.attempt, RiskEvidence.created_at, RiskEvidence.id)
                )
            ).all()
        )
        if risks
        else []
    )
    by_risk: dict[uuid.UUID, list[tuple[RiskEvidence, str | None]]] = {}
    for row, key in evidence_rows:
        by_risk.setdefault(row.risk_id, []).append((row, key))

    summary = analysis.summary or {}
    page_source = version.page_source.value if version.page_source else None
    if page_source is None:
        page_note = "Страницы документа не определены — в доказательствах не указываются."
    elif page_source == "pdf_native":
        page_note = None
    else:
        page_note = "Страницы DOCX/DOC приблизительны (по разметке Word или LibreOffice)."

    report_risks: list[ReportRisk] = []
    rejected: list[ReportRisk] = []
    for r in risks:
        rows = by_risk.get(r.id, [])
        last = max((e.attempt for e, _k in rows), default=0)
        item = ReportRisk(
            key=r.risk_key,
            kind=KIND.get(r.kind.value, r.kind.value),
            title=r.title,
            category=category_label(r.category),
            severity=SEVERITY.get(r.severity.value, r.severity.value),
            validation_status=r.validation_status.value,
            validation=VALIDATION.get(r.validation_status.value, r.validation_status.value),
            affected_party=AFFECTED.get(r.affected_party.value, r.affected_party.value),
            description=r.description,
            reason=r.reason,
            consequences=r.consequences,
            recommendation=r.recommendation,
            suggested_revision=r.suggested_revision,
            model_confidence=r.model_confidence,
            evidence=[
                ReportEvidence(
                    fragment_key=key or e.claimed_fragment_key,
                    quote=e.quote,
                    pages=_pages(e.page_start, e.page_end),
                    clause_ref=e.clause_ref,
                    match=MATCH.get(e.match_status.value, e.match_status.value),
                    relevance=RELEVANCE.get(e.relevance_status.value, e.relevance_status.value),
                    rationale=e.relevance_rationale,
                )
                for e, key in rows
                if e.attempt == last
            ],
            search_scope=r.search_scope,
            legal_reference=bool((r.validation_notes or {}).get("legal_reference_unverified")),
        )
        (rejected if r.validation_status is ValidationStatus.rejected else report_risks).append(item)

    counts_raw = summary.get("counts") or {}
    by_status = counts_raw.get("by_validation_status") or {}
    counts = [
        ("Всего рисков", str(counts_raw.get("total", len(risks)))),
        ("Подтверждены", str(by_status.get("verified_match", 0))),
        ("Требуют проверки", str(by_status.get("needs_review", 0))),
        ("Отклонены (не подтверждены)", str(by_status.get("rejected", 0))),
        ("Отсутствующие условия", str(counts_raw.get("missing_clauses", 0))),
        ("Противоречия", str(counts_raw.get("contradictions", 0))),
    ]
    party_summary = summary.get("party") or {}
    party_name = (
        analysis.party_name if analysis.party is AnalysisParty.named else party_summary.get("resolved_name")
    )
    return Report(
        analysis_id=analysis.id,
        generated_at=datetime.now(UTC),
        document_title=document.title,
        version_no=version.version_no,
        page_note=page_note,
        analysis_status=STATUS.get(analysis.status.value, analysis.status.value),
        created_at=analysis.created_at,
        finished_at=analysis.finished_at,
        party=PARTY.get(analysis.party.value, analysis.party.value),
        party_name=party_name,
        depth=DEPTH.get(analysis.depth.value, analysis.depth.value),
        categories=[category_label(c) for c in analysis.categories],
        model=_model_lines(analysis.config_snapshot or {}),
        statement=str(summary.get("statement", "")),
        counts=counts,
        coverage=_coverage_lines(analysis.coverage),
        limitations=[str(x) for x in summary.get("limitations", [])],
        disclaimer=str(summary.get("disclaimer", "")),
        risks=report_risks,
        rejected=rejected,
    )
