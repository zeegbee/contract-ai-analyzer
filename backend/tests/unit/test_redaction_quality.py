"""Качество обнаружения на размеченном синтетическом наборе (`tests.redaction_corpus`).

Пороги — защита от регрессий, а не оценка качества на реальных договорах: набор собран из
шаблонов, близких к правилам детекторов. Фактические числа — в IMPLEMENTATION_STATUS (этап 10).
"""

from __future__ import annotations

from tests.redaction_corpus import evaluate

# Категории с контрольными суммами и точными форматами — без пропусков на синтетическом наборе.
STRICT = (
    "inn_org",
    "inn_person",
    "ogrn",
    "kpp",
    "bik",
    "bank_account",
    "bank_card",
    "snils",
    "email",
    "passport_ru",
)


def test_detection_quality_on_synthetic_corpus() -> None:
    metrics = evaluate()
    rows = {row[0]: row for row in metrics.rows()}
    for category in STRICT:
        _c, expected, covered, _r, detected, _p = rows[category]
        assert expected and covered == expected, category
        assert detected and metrics.correct[category] == detected, category
    total_expected, total_covered = sum(metrics.expected.values()), sum(metrics.covered.values())
    total_detected, total_correct = sum(metrics.detected.values()), sum(metrics.correct.values())
    assert total_covered / total_expected >= 0.95
    assert total_correct / total_detected >= 0.95
    # Названия без кавычек и формы собственности распознаются не всегда (известное ограничение).
    assert rows["org"][3] >= 0.8
    assert rows["person"][3] >= 0.95
