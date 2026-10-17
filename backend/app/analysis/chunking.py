"""Разбиение текста версии на чанки с перекрытием (ARCHITECTURE §9.2 п.1, CHUNKER_VERSION).

- Последовательные фрагменты группируются по бюджету токенов модели; фрагмент не режется.
- Перекрытие — последние фрагменты предыдущего чанка общей длиной до ~15 % бюджета, но не менее
  одного фрагмента. Каждый чанк добавляет хотя бы один новый фрагмент.
- `chunk_key = sha256(version_id, seq_start, seq_end, CHUNKER_VERSION, бюджет)` — повторное
  планирование той же версии с тем же бюджетом даёт те же чанки и ключи.

Токены оцениваются по длине текста: CHARS_PER_TOKEN = 3 символа на токен — консервативно для
кириллицы (у токенизаторов GPT/Llama русский текст ≈ 2,5–4 символа на токен). Это оценка для
планирования, а не измерение: фактический расход берётся из ответа провайдера, если он его сообщает.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.analysis import CHUNKER_VERSION
from app.models.enums import AnalysisDepth

CHARS_PER_TOKEN = 3.0
# Контекст модели, если каталог его не знает (ARCHITECTURE §9.2: консервативные 8k).
DEFAULT_CONTEXT_TOKENS = 8192
# Системный промпт, инструкции, краткая структура договора и разметка фрагментов.
PROMPT_RESERVE_TOKENS = 2500
MIN_TEXT_BUDGET_TOKENS = 1000
OVERLAP_SHARE = 0.15
SEPARATOR_CHARS = 2
# Верхняя граница текста на чанк: чем глубже анализ, тем меньше текста на один вызов модели.
DEPTH_TEXT_CAP_TOKENS = {
    AnalysisDepth.quick: 12_000,
    AnalysisDepth.standard: 8_000,
    AnalysisDepth.deep: 4_000,
}


class ContextTooSmall(ValueError):
    """Контекст модели не вмещает даже минимальный чанк вместе с лимитом ответа."""


@dataclass(frozen=True)
class FragmentSize:
    seq: int
    chars: int


@dataclass(frozen=True)
class PlannedChunk:
    index: int
    key: str
    seq_start: int
    seq_end: int
    # Первый фрагмент перекрытия с предыдущим чанком (он же seq_start); None — перекрытия нет.
    overlap_from_seq: int | None
    # Последний фрагмент перекрытия (= seq_end предыдущего чанка); None — перекрытия нет.
    overlap_to_seq: int | None
    chars: int

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.chars)


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def text_budget_tokens(context_window: int | None, max_output_tokens: int, depth: AnalysisDepth) -> int:
    context = context_window or DEFAULT_CONTEXT_TOKENS
    available = context - PROMPT_RESERVE_TOKENS - max_output_tokens
    if available < MIN_TEXT_BUDGET_TOKENS:
        raise ContextTooSmall(f"context={context}, output={max_output_tokens}, available={available}")
    return min(available, DEPTH_TEXT_CAP_TOKENS[depth])


def chunk_key(version_id: uuid.UUID, seq_start: int, seq_end: int, budget_tokens: int) -> str:
    raw = f"{version_id}:{seq_start}:{seq_end}:{CHUNKER_VERSION}:{budget_tokens}"
    return hashlib.sha256(raw.encode()).hexdigest()


def plan_chunks(
    version_id: uuid.UUID, fragments: Sequence[FragmentSize], budget_tokens: int
) -> list[PlannedChunk]:
    items = sorted(fragments, key=lambda f: f.seq)
    budget = int(budget_tokens * CHARS_PER_TOKEN)
    overlap_budget = int(budget * OVERLAP_SHARE)
    chunks: list[PlannedChunk] = []
    next_new = 0  # индекс первого ещё не включённого фрагмента
    prev_start_idx = -1
    while next_new < len(items):
        start = next_new
        size = 0
        if chunks:
            # Перекрытие: идём назад от последнего фрагмента предыдущего чанка, не доходя до его начала.
            start = next_new - 1
            size = items[start].chars
            while (
                start - 1 > prev_start_idx
                and size + SEPARATOR_CHARS + items[start - 1].chars <= overlap_budget
            ):
                start -= 1
                size += SEPARATOR_CHARS + items[start].chars
        prev_start_idx = start
        end = next_new
        size += (SEPARATOR_CHARS if size else 0) + items[end].chars
        while end + 1 < len(items) and size + SEPARATOR_CHARS + items[end + 1].chars <= budget:
            end += 1
            size += SEPARATOR_CHARS + items[end].chars
        seq_start, seq_end = items[start].seq, items[end].seq
        overlap = chunks[-1].seq_end if chunks else None
        chunks.append(
            PlannedChunk(
                index=len(chunks),
                key=chunk_key(version_id, seq_start, seq_end, budget_tokens),
                seq_start=seq_start,
                seq_end=seq_end,
                overlap_from_seq=seq_start if overlap is not None else None,
                overlap_to_seq=overlap,
                chars=size,
            )
        )
        next_new = end + 1
    return chunks
