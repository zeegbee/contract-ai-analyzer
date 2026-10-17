"""Серверная проверка доказательств (ARCHITECTURE §10).

Шаг A (детерминированно): фрагмент существует в этой версии и входит в текст, который видела
модель; цитата дословно (или после документированной нормализации, `app.analysis.quotes`) входит в
текст фрагмента или в объединение с соседним фрагментом (цитата через границу абзаца/страницы).
Страница и пункт берутся из метаданных версии по смещениям совпадения — всё, что модель могла
написать о страницах, игнорируется.

Шаг B (смысловая поддержка) выполняет верификатор; здесь — правила поверх его вердикта и итоговый
статус риска (§10.3).
"""

from __future__ import annotations

import bisect
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.analysis.quotes import find_quote, is_too_short, normalize, unescape_quote
from app.models.enums import MatchStatus, RelevanceStatus, RiskKind, ValidationStatus

SEPARATOR = "\n\n"  # как в каноническом тексте (app.ingestion.assemble)
MATCHED = frozenset({MatchStatus.exact, MatchStatus.normalized})


@dataclass(frozen=True)
class FragmentRef:
    id: uuid.UUID
    seq: int
    key: str
    kind: str
    text: str
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeqScope:
    """Фрагменты, которые модель видела в запросе (диапазоны seq включительно)."""

    ranges: tuple[tuple[int, int], ...]

    def __contains__(self, seq: object) -> bool:
        return isinstance(seq, int) and any(a <= seq <= b for a, b in self.ranges)

    @classmethod
    def of(cls, seqs: Iterable[int]) -> SeqScope:
        ranges: list[tuple[int, int]] = []
        for seq in sorted(set(seqs)):
            if ranges and seq == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], seq)
            else:
                ranges.append((seq, seq))
        return cls(tuple(ranges))

    def union(self, other: SeqScope) -> SeqScope:
        seqs = [s for a, b in (*self.ranges, *other.ranges) for s in range(a, b + 1)]
        return SeqScope.of(seqs)

    def to_json(self) -> list[list[int]]:
        return [[a, b] for a, b in self.ranges]

    @classmethod
    def from_json(cls, value: Iterable[Sequence[int]]) -> SeqScope:
        return cls(tuple((int(a), int(b)) for a, b in value))

    def seqs(self) -> list[int]:
        return [s for a, b in self.ranges for s in range(a, b + 1)]


class FragmentIndex:
    def __init__(self, fragments: Sequence[FragmentRef], pages: Sequence[tuple[int, int, int]] = ()) -> None:
        self.fragments = sorted(fragments, key=lambda f: f.seq)
        self.by_key = {f.key: f for f in self.fragments}
        self.by_seq = {f.seq: f for f in self.fragments}
        # (page_no, char_start, char_end) по возрастанию начала; страницы нулевой длины допустимы.
        self.pages = sorted(pages, key=lambda p: (p[1], p[0]))
        self._page_starts = [p[1] for p in self.pages]

    def page_at(self, offset: int) -> int | None:
        if not self.pages:
            return None
        i = bisect.bisect_right(self._page_starts, offset) - 1
        return self.pages[i][0] if i >= 0 else None

    def page_range(self, start: int, end: int, span: Sequence[FragmentRef]) -> tuple[int | None, int | None]:
        if self.pages:
            return self.page_at(start), self.page_at(max(start, end - 1))
        # Страниц версии нет — только то, что достоверно известно о фрагментах (обычно тоже null).
        return span[0].page_start, span[-1].page_end


@dataclass
class EvidenceMatch:
    status: MatchStatus
    fragment: FragmentRef | None
    span: list[FragmentRef] = field(default_factory=list)
    start: int | None = None
    end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    clause_ref: str | None = None
    quote_normalized: str | None = None
    notes: list[str] = field(default_factory=list)


def match_evidence(index: FragmentIndex, scope: SeqScope, fragment_key: str, quote: str) -> EvidenceMatch:
    text = unescape_quote(quote)
    normalized = normalize(text).text
    fragment = index.by_key.get(fragment_key)
    if fragment is None:
        return EvidenceMatch(
            MatchStatus.fragment_mismatch, None, quote_normalized=normalized, notes=["fragment_not_found"]
        )
    if fragment.seq not in scope:
        return EvidenceMatch(
            MatchStatus.fragment_mismatch,
            fragment,
            quote_normalized=normalized,
            notes=["fragment_not_in_model_context"],
        )
    if is_too_short(normalized):
        return EvidenceMatch(
            MatchStatus.too_short, fragment, quote_normalized=normalized, notes=["quote_too_short"]
        )

    found = _find_in_window(index, scope, fragment, text)
    if found is not None:
        status, span, start, end, notes = found
        page_start, page_end = index.page_range(start, end, span)
        return EvidenceMatch(
            status,
            fragment,
            span=span,
            start=start,
            end=end,
            page_start=page_start,
            page_end=page_end,
            # Пункт — только тот, что достоверно определён для фрагмента, с которого начинается цитата.
            clause_ref=span[0].clause_ref,
            quote_normalized=normalized,
            notes=notes,
        )
    notes = ["quote_not_found"]
    for seq in scope.seqs():
        other = index.by_seq.get(seq)
        if other is not None and other.seq != fragment.seq and find_quote(other.text, text):
            notes.append(f"quote_found_in:{other.key}")
            break
    return EvidenceMatch(MatchStatus.not_found, fragment, quote_normalized=normalized, notes=notes)


def _find_in_window(
    index: FragmentIndex, scope: SeqScope, fragment: FragmentRef, quote: str
) -> tuple[MatchStatus, list[FragmentRef], int, int, list[str]] | None:
    m = find_quote(fragment.text, quote)
    if m is not None:
        notes = ["dehyphenated"] if m.dehyphenated else []
        status = MatchStatus.exact if m.kind == "exact" else MatchStatus.normalized
        return status, [fragment], fragment.char_start + m.start, fragment.char_start + m.end, notes
    # Цитата через границу соседних фрагментов (только видимых модели и идущих подряд).
    window = [fragment]
    prev, nxt = index.by_seq.get(fragment.seq - 1), index.by_seq.get(fragment.seq + 1)
    if prev is not None and prev.seq in scope:
        window.insert(0, prev)
    if nxt is not None and nxt.seq in scope:
        window.append(nxt)
    if len(window) == 1:
        return None
    combined = SEPARATOR.join(f.text for f in window)
    m = find_quote(combined, quote)
    if m is None:
        return None
    base = window[0].char_start
    start, end = base + m.start, base + m.end
    span = [f for f in window if f.char_start < end and f.char_end > start]
    if fragment not in span:
        return None  # цитата целиком в соседнем фрагменте — модель указала не тот фрагмент
    status = MatchStatus.exact if m.kind == "exact" else MatchStatus.normalized
    notes = ["spans_fragments"] + (["dehyphenated"] if m.dehyphenated else [])
    return status, span, start, end, notes


# ------------------------------------------------------------------ шаг B: правила и итоговый статус

_RELEVANCE_RANK = {
    RelevanceStatus.does_not_support: 0,
    RelevanceStatus.unchecked: 1,
    RelevanceStatus.partially_supports: 2,
    RelevanceStatus.supports: 3,
}


def apply_relevance_rules(
    verdict: RelevanceStatus, span_kinds: Sequence[str]
) -> tuple[RelevanceStatus, bool]:
    """Цитата только из заголовков подтверждает риск не выше чем `partially_supports`.

    Возвращает (статус, применено ли правило).
    """
    if span_kinds and all(k == "heading" for k in span_kinds) and verdict is RelevanceStatus.supports:
        return RelevanceStatus.partially_supports, True
    return verdict, False


@dataclass(frozen=True)
class EvidenceState:
    match_status: MatchStatus
    relevance_status: RelevanceStatus
    fragment_seq: int | None


def final_status(
    kind: RiskKind, evidence: Sequence[EvidenceState], *, withdrawn: bool = False
) -> tuple[ValidationStatus, list[str]]:
    """Итоговый статус риска по актуальным доказательствам (ARCHITECTURE §10.3)."""
    if withdrawn:
        return ValidationStatus.rejected, ["withdrawn_by_model"]
    if kind is RiskKind.missing_clause:
        # Отсутствующее условие не имеет цитаты по определению — всегда требует проверки человеком.
        return ValidationStatus.needs_review, ["missing_clause_requires_review"]
    if not evidence:
        return ValidationStatus.rejected, ["no_evidence"]
    if any(e.match_status is MatchStatus.fragment_mismatch for e in evidence):
        return ValidationStatus.rejected, ["fragment_mismatch"]
    matched = [e for e in evidence if e.match_status in MATCHED]
    if not matched:
        return ValidationStatus.rejected, ["no_matching_quote"]
    if all(e.relevance_status is RelevanceStatus.does_not_support for e in matched):
        return ValidationStatus.rejected, ["quotes_do_not_support_risk"]

    notes: list[str] = []
    if len(matched) < len(evidence):
        notes.append("some_quotes_not_matched")
    if any(e.relevance_status is RelevanceStatus.does_not_support for e in matched):
        notes.append("some_quotes_do_not_support_risk")
    supporting = [e for e in matched if e.relevance_status is RelevanceStatus.supports]
    if not supporting:
        best = max(matched, key=lambda e: _RELEVANCE_RANK[e.relevance_status]).relevance_status
        notes.append("relevance_unchecked" if best is RelevanceStatus.unchecked else "relevance_partial")
    if kind is RiskKind.contradiction:
        relevant = {
            e.fragment_seq
            for e in matched
            if e.relevance_status in (RelevanceStatus.supports, RelevanceStatus.partially_supports)
        }
        if len({e.fragment_seq for e in matched}) < 2 or len(relevant) < 2:
            notes.append("contradiction_needs_two_fragments")
    if notes:
        return ValidationStatus.needs_review, notes
    return ValidationStatus.verified_match, []
