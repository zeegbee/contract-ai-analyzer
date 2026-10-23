"""Дедупликация рисков из перекрывающихся и соседних чанков и межраздельного прохода (§9.2 п.5).

Два риска объединяются, если у них одинаковые тип и категория и
- доказательства ссылаются хотя бы на один общий фрагмент и названия похожи (сходство ≥ 0,3), или
- названия почти совпадают (сходство ≥ 0,75), или
- это отсутствующие условия (без доказательств) со сходством названий ≥ 0,5.
Противоречия (kind=contradiction) сравниваются без учёта категории; два общих фрагмента
доказательств достаточно для объединения.
Сходство — коэффициент Жаккара по основам слов (первые 5 букв, без регистра и пунктуации).
При объединении сохраняются все доказательства и источники, severity — максимальная,
уверенность модели — максимальная; основной текст берётся у самого серьёзного риска.
Код правила организации (этап 11) — у основного риска, а если у него нет — у самого серьёзного из
сливаемых; остальные коды сохраняются в `merged_rules`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.analysis.quotes import normalize
from app.analysis.schemas import EvidenceItem, RiskItem
from app.models.enums import RiskKind, RiskSeverity

SEVERITY_RANK = {
    RiskSeverity.critical: 4,
    RiskSeverity.high: 3,
    RiskSeverity.medium: 2,
    RiskSeverity.low: 1,
    RiskSeverity.informational: 0,
}
_WORD = re.compile(r"[0-9a-zа-яё]+")
_STEM = 5


@dataclass
class Candidate:
    risk: RiskItem
    # Источники: ID чанков (str) или "cross_section".
    sources: set[str]
    # Фрагменты (seq), которые модель видела в запросах-источниках — для проверки доказательств.
    scope_seqs: set[int]
    # Модель указала код правила, которого нет в анализе (код уже снят с риска).
    rule_code_unknown: bool = False


@dataclass
class MergedRisk:
    risk: RiskItem
    evidence: list[EvidenceItem]
    sources: set[str]
    scope_seqs: set[int]
    merged_count: int = 1
    severities: list[RiskSeverity] = field(default_factory=list)
    # Коды правил сливаемых рисков, кроме выбранного (этап 11).
    merged_rules: list[str] = field(default_factory=list)
    rule_code_unknown: bool = False


def _stems(title: str) -> set[str]:
    return {w[:_STEM] for w in _WORD.findall(title.lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _same(a: Candidate, b: Candidate) -> bool:
    ra, rb = a.risk, b.risk
    if ra.kind is not rb.kind:
        return False
    sim = similarity(ra.title, rb.title)
    shared = {e.fragment_id for e in ra.evidence} & {e.fragment_id for e in rb.evidence}
    if ra.kind is RiskKind.contradiction:
        # Противоречие определяется парой условий; категорию модели ставят по-разному
        # (contradiction / payment …), поэтому она не учитывается.
        return len(shared) >= 2 or (bool(shared) and sim >= 0.3) or sim >= 0.75
    if ra.category is not rb.category:
        return False
    if ra.kind is RiskKind.missing_clause and not ra.evidence and not rb.evidence:
        return sim >= 0.5
    return (bool(shared) and sim >= 0.3) or sim >= 0.75


def _merge_evidence(members: Sequence[Candidate]) -> list[EvidenceItem]:
    """Объединение доказательств: из цитат одного фрагмента, где одна содержит другую, — более полная."""
    kept: list[tuple[EvidenceItem, str]] = []
    for member in members:
        for item in member.risk.evidence:
            norm = normalize(item.quote).text
            duplicate = False
            for i, (other, other_norm) in enumerate(kept):
                if other.fragment_id != item.fragment_id:
                    continue
                if norm in other_norm:
                    duplicate = True
                    break
                if other_norm in norm:
                    kept[i] = (item, norm)
                    duplicate = True
                    break
            if not duplicate:
                kept.append((item, norm))
    return [item for item, _norm in kept]


def deduplicate(candidates: Sequence[Candidate]) -> list[MergedRisk]:
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            if find(i) != find(j) and _same(candidates[i], candidates[j]):
                parent[find(j)] = find(i)

    groups: dict[int, list[Candidate]] = {}
    for i, candidate in enumerate(candidates):
        groups.setdefault(find(i), []).append(candidate)

    merged: list[MergedRisk] = []
    for members in groups.values():
        main = max(
            members,
            key=lambda c: (SEVERITY_RANK[c.risk.severity], c.risk.model_confidence, len(c.risk.description)),
        )
        evidence = _merge_evidence([main, *[m for m in members if m is not main]])
        ranked = sorted(members, key=lambda c: (c is not main, -SEVERITY_RANK[c.risk.severity]))
        codes = list(dict.fromkeys(c.risk.rule_code for c in ranked if c.risk.rule_code))
        risk = main.risk.model_copy(
            update={
                "model_confidence": max(m.risk.model_confidence for m in members),
                "rule_code": codes[0] if codes else None,
            }
        )
        merged.append(
            MergedRisk(
                risk=risk,
                evidence=evidence[:10],
                sources=set().union(*(m.sources for m in members)),
                scope_seqs=set().union(*(m.scope_seqs for m in members)),
                merged_count=len(members),
                severities=[m.risk.severity for m in members],
                merged_rules=codes[1:],
                rule_code_unknown=any(m.rule_code_unknown for m in members),
            )
        )
    return merged
