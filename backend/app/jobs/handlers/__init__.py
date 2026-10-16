"""Обработчики задач. Импорт модуля регистрирует их в `app.jobs.registry`."""

from app.jobs.handlers import documents

__all__ = ["documents"]
