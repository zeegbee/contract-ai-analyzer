"""Реестр обработчиков задач. Обработчики регистрируются в `app.jobs.handlers`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.storage import LocalStorage


@dataclass(slots=True)
class JobContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    job_id: str
    attempt: int
    max_attempts: int = 1

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


class JobError(Exception):
    """Ошибка задачи с безопасным кодом. retryable=False — повтор бессмыслен."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


Handler = Callable[[JobContext, dict[str, Any]], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {}


def register(job_type: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        if job_type in _HANDLERS:
            raise RuntimeError(f"duplicate job handler: {job_type}")
        _HANDLERS[job_type] = func
        return func

    return decorator


def get_handler(job_type: str) -> Handler | None:
    return _HANDLERS.get(job_type)


def registered_types() -> list[str]:
    return sorted(_HANDLERS)
