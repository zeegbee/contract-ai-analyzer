"""Обработчики задач. Импорт модуля регистрирует их в `app.jobs.registry`."""

from app.jobs.handlers import analyses, documents

__all__ = ["analyses", "documents"]
