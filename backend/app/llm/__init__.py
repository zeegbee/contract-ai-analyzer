"""Слой LLM-провайдеров (ARCHITECTURE §12).

Бизнес-логика работает только с `LLMProvider` (`app.llm.base`) и нормализованными ошибками
(`app.llm.errors`); конкретные адаптеры — `openrouter`, `ollama`, `mock`.
"""
