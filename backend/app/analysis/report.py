"""Покрытие и итоговая сводка анализа (ARCHITECTURE §9.2 п.8–9).

Сводка никогда не утверждает, что договор безопасен: при отсутствии подтверждённых рисков она
говорит только о проанализированных разделах и перечисляет ограничения.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.models.enums import AnalysisParty, ChunkStatus, PageSource, RiskKind, RiskSeverity, ValidationStatus

DISCLAIMER = (
    "Результат подготовлен автоматически с помощью языковой модели и не заменяет консультацию юриста. "
    "Проверьте выводы, цитаты и рекомендации перед использованием."
)
NO_CONFIRMED_RISKS = (
    "В проанализированных разделах модель не выявила рисков с подтверждёнными цитатами. "
    "Это не означает, что договор безопасен или не содержит рисков."
)

_BASE_LIMITATIONS = [
    "Оценка уверенности (model_confidence) — субъективная оценка модели, а не вероятность.",
    "Предлагаемые редакции — проекты для обсуждения, а не готовый юридический текст.",
    "Ссылки на нормы права, если они есть в тексте рисков, сервером не проверяются и требуют "
    "проверки юристом.",
    "Смысловая связь цитаты с риском оценена моделью-верификатором и требует проверки специалистом.",
]


@dataclass(frozen=True)
class ChunkInfo:
    index: int
    seq_start: int
    seq_end: int
    status: ChunkStatus
    error_code: str | None


@dataclass(frozen=True)
class RiskInfo:
    kind: RiskKind
    severity: RiskSeverity
    validation_status: ValidationStatus
    verifier: str | None
    quote_retry: str | None
    rule_code: str | None = None


def build_coverage(
    *,
    fragment_keys: dict[int, str],
    chunks: Sequence[ChunkInfo],
    sections: Sequence[dict[str, Any]],
    cross_section: dict[str, Any],
    risks: Sequence[RiskInfo],
    page_source: PageSource | None,
) -> tuple[dict[str, Any], list[str]]:
    """Возвращает (coverage, коды предупреждений, из-за которых статус — completed_with_warnings)."""
    analyzed: set[int] = set()
    for c in chunks:
        if c.status is ChunkStatus.done:
            analyzed.update(s for s in range(c.seq_start, c.seq_end + 1) if s in fragment_keys)
    section_items = []
    for s in sections:
        seqs = [q for q in range(s["start_seq"], s["end_seq"] + 1) if q in fragment_keys]
        done = sum(1 for q in seqs if q in analyzed)
        state = "analyzed" if seqs and done == len(seqs) else ("partial" if done else "not_analyzed")
        section_items.append(
            {
                "title": s["title"],
                "start_key": s["start_key"],
                "clause_ref": s.get("clause_ref"),
                "status": state,
            }
        )
    failed = [c for c in chunks if c.status is ChunkStatus.failed]
    skipped = [c for c in chunks if c.status is ChunkStatus.skipped]
    verifier_counts = Counter(r.verifier or "not_run" for r in risks)
    retry_counts = Counter(r.quote_retry for r in risks if r.quote_retry)
    warnings: list[str] = []
    if failed:
        warnings.append("chunks_failed")
    if skipped:
        warnings.append("chunks_skipped")
    if any(s["status"] != "analyzed" for s in section_items):
        warnings.append("sections_not_fully_analyzed")
    if cross_section.get("status") == "failed" or cross_section.get("reason") == "token_budget_exhausted":
        warnings.append("cross_section_incomplete")
    if any(k.startswith(("unavailable", "failed", "skipped")) for k in verifier_counts):
        warnings.append("verification_incomplete")
    if retry_counts.get("skipped_limit") or retry_counts.get("skipped_budget"):
        warnings.append("quote_retry_skipped")

    coverage = {
        "fragments_total": len(fragment_keys),
        "fragments_analyzed": len(analyzed),
        "chunks_total": len(chunks),
        "chunks_done": sum(1 for c in chunks if c.status is ChunkStatus.done),
        "chunks_failed": [_chunk_ref(c, fragment_keys) for c in failed],
        "chunks_skipped": [_chunk_ref(c, fragment_keys) for c in skipped],
        "sections": section_items,
        "cross_section": {k: v for k, v in cross_section.items() if k in ("status", "reason", "code")},
        "verification": dict(verifier_counts),
        "quote_retries": dict(retry_counts),
        "page_source": page_source.value if page_source else None,
        "pages_approximate": page_source in (PageSource.libreoffice_render, PageSource.docx_rendered_breaks),
    }
    return coverage, warnings


def _chunk_ref(c: ChunkInfo, keys: dict[int, str]) -> dict[str, Any]:
    return {
        "index": c.index,
        "from": keys.get(c.seq_start),
        "to": keys.get(c.seq_end),
        "code": c.error_code,
    }


_WARNING_TEXT = {
    "chunks_failed": "Часть текста не проанализирована: модель не вернула корректный результат для "
    "некоторых частей договора (см. покрытие).",
    "chunks_skipped": "Часть текста не проанализирована: исчерпан лимит токенов анализа.",
    "sections_not_fully_analyzed": "Не все разделы договора проанализированы полностью (см. покрытие).",
    "cross_section_incomplete": "Поиск межраздельных противоречий и отсутствующих условий не выполнен "
    "или выполнен не полностью.",
    "verification_incomplete": "Смысловая проверка цитат выполнена не для всех рисков — такие риски "
    "требуют проверки.",
    "quote_retry_skipped": "Для части рисков без найденной цитаты повторный запрос не выполнялся "
    "(лимит) — они отклонены или требуют проверки.",
}


def build_summary(
    *,
    party: AnalysisParty,
    party_name: str | None,
    party_label: str | None,
    snapshot: dict[str, Any],
    categories: Sequence[str],
    depth: str,
    risks: Sequence[RiskInfo],
    warnings: Sequence[str],
    injection_hits: Sequence[dict[str, Any]],
    structure: dict[str, Any] | None,
    page_source: PageSource | None,
    rules: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    by_status = Counter(r.validation_status.value for r in risks)
    shown = [r for r in risks if r.validation_status is not ValidationStatus.rejected]
    verified = [r for r in risks if r.validation_status is ValidationStatus.verified_match]
    by_severity = Counter(r.severity.value for r in shown)
    if verified:
        statement = (
            f"Рисков с подтверждёнными цитатами: {len(verified)}; требуют проверки: "
            f"{by_status.get(ValidationStatus.needs_review.value, 0)}."
        )
    else:
        statement = NO_CONFIRMED_RISKS
    limitations = [_WARNING_TEXT[w] for w in warnings if w in _WARNING_TEXT]
    if party is AnalysisParty.unknown:
        limitations.append(
            "Сторона анализа не указана: риски описаны нейтрально, без оценки выгодности условий "
            "для конкретной стороны."
        )
    elif party in (AnalysisParty.party_a, AnalysisParty.party_b) and not party_label:
        limitations.append(
            "Наименование выбранной стороны не определено по тексту: сторона A — первая, сторона B — "
            "вторая сторона, названная в преамбуле договора."
        )
    if page_source is None:
        limitations.append("Страницы документа не определены — в доказательствах они не указываются.")
    elif page_source in (PageSource.libreoffice_render, PageSource.docx_rendered_breaks):
        limitations.append(
            "Номера страниц DOCX/DOC приблизительны (по разметке Word или LibreOffice) и могут "
            "отличаться от вашего просмотра."
        )
    if snapshot.get("verifier", {}).get("same_as_analysis", True):
        limitations.append("Смысловая проверка цитат выполнена той же моделью, что и анализ.")
    if injection_hits:
        limitations.append(
            "В тексте договора найдены фразы, похожие на инструкции для модели (возможная попытка "
            "prompt injection). Они не выполнялись, но результаты по соответствующим фрагментам стоит "
            "проверить особенно внимательно."
        )
    if rules:
        limitations.append(
            "Применены правила рисков организации: модель проверяла договор по их описанию. Отметка "
            "правила у риска — вывод модели; отсутствие риска по правилу не доказывает, что договор "
            "ему соответствует."
        )
    limitations.extend(_BASE_LIMITATIONS)
    by_rule = Counter(r.rule_code for r in shown if r.rule_code)
    model = snapshot.get("model", {})
    provider = snapshot.get("provider", {})
    verifier = snapshot.get("verifier", {})
    return {
        "party": {"code": party.value, "name": party_name, "resolved_name": party_label},
        "model": {
            "display_name": model.get("display_name"),
            "external_model_id": model.get("external_model_id"),
            "provider_name": provider.get("name"),
            "provider_type": provider.get("type"),
            "is_cloud": provider.get("is_cloud"),
        },
        "verifier_model": {
            "same_as_analysis": verifier.get("same_as_analysis", True),
            "display_name": verifier.get("model", {}).get("display_name", model.get("display_name")),
            "external_model_id": verifier.get("model", {}).get(
                "external_model_id", model.get("external_model_id")
            ),
        },
        "categories": list(categories),
        "depth": depth,
        "contract": {
            "contract_type": (structure or {}).get("contract_type"),
            "subject": (structure or {}).get("subject"),
            "parties": [
                {"label": p["label"], "name": p["name"], "role": p.get("role")}
                for p in (structure or {}).get("parties", [])
                if p.get("found_in_text")
            ],
        },
        "counts": {
            "total": len(risks),
            "shown": len(shown),
            "by_validation_status": dict(by_status),
            "by_severity": dict(by_severity),
            "missing_clauses": sum(1 for r in shown if r.kind is RiskKind.missing_clause),
            "contradictions": sum(1 for r in shown if r.kind is RiskKind.contradiction),
            "by_rule": dict(by_rule),
        },
        # Применённые правила организации (из snapshot, с ревизией) и число показанных рисков по каждому.
        "risk_rules": [
            {
                "code": r["code"],
                "title": r["title"],
                "category": r["category"],
                "kind": r["kind"],
                "revision": r.get("revision"),
                "risks": by_rule.get(r["code"], 0),
            }
            for r in rules
        ],
        "statement": statement,
        "warnings": list(warnings),
        "prompt_injection": list(injection_hits),
        "limitations": limitations,
        "disclaimer": DISCLAIMER,
    }
