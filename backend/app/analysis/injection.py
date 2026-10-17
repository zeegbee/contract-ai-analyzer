"""Эвристический детектор prompt injection в тексте договора (ARCHITECTURE §11).

Только предупреждение в отчёте: поведение конвейера не меняется. Основная защита — текст договора
передаётся модели как данные в разделителях, у модели нет инструментов и секретов, ответ строго
валидируется, а статусы проверки, страницы и права вычисляет сервер.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(pattern, re.IGNORECASE))
    for code, pattern in (
        (
            "ignore_instructions",
            r"\b(игнорир\w*|забудь\w*|не\s+учитыва\w*|отмени\w*)\s+(все\s+|всех\s+)?"
            r"(предыдущ\w*|прежн\w*|вышеуказанн\w*|системн\w*|данн\w*\s+ранее)?\s*"
            r"(инструкц\w*|указани\w*|правил\w*|промпт\w*)",
        ),
        (
            "ignore_instructions",
            r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(the\s+)?"
            r"(previous|prior|above|earlier|system)?\s*(instructions?|prompts?|rules)",
        ),
        ("role_override", r"\b(you\s+are\s+now|act\s+as|ты\s+теперь|представь,?\s+что\s+ты)\b"),
        ("role_marker", r"(^|\n)\s*(system|assistant|developer|система|ассистент)\s*:"),
        (
            "prompt_markup",
            r"</?\s*(fragment|system|instructions?|prompt)\b[^>]*>|\[/?INST\]|<\|im_(start|end)\|>",
        ),
        ("system_prompt_reference", r"\b(system\s+prompt|системн\w+\s+(промпт|инструкц\w*|сообщени\w*))"),
        (
            "verdict_manipulation",
            r"\b(договор\s+(является\s+)?безопас\w*|рисков\s+нет|не\s+(сообщай|указывай|упоминай|выявляй)\w*"
            r"\s+(о\s+)?риск\w*|report\s+no\s+risks|(contract|agreement)\s+is\s+safe)",
        ),
        ("output_manipulation", r"\b(верни|выведи|ответь)\s+(только\s+)?(пуст\w+|json|ответ)\b"),
    )
)


@dataclass(frozen=True)
class InjectionHit:
    fragment_key: str
    code: str


def scan(fragments: Iterable[tuple[str, str]], *, limit: int = 50) -> list[InjectionHit]:
    """Находит подозрительные инструкции. На входе — пары (ключ фрагмента, текст).

    Результат содержит только ключи фрагментов и коды правил — без текста договора.
    """
    hits: list[InjectionHit] = []
    for key, text in fragments:
        seen: set[str] = set()
        for code, pattern in _PATTERNS:
            if code not in seen and pattern.search(text):
                seen.add(code)
                hits.append(InjectionHit(key, code))
                if len(hits) >= limit:
                    return hits
    return hits
