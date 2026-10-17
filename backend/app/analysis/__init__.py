"""Конвейер анализа договора (ARCHITECTURE §9–11).

Версии компонентов сохраняются в `Analysis.config_snapshot`: по ним видно, какими промптами, схемами,
чанкером и нормализатором получен результат. При изменении поведения компонента версия повышается.
"""

PIPELINE_VERSION = "pipeline-1"
SCHEMA_VERSION = "schema-1"
CHUNKER_VERSION = "chunker-1"
NORMALIZER_VERSION = "normalizer-1"
INJECTION_DETECTOR_VERSION = "injection-1"
