"""Структурированное логирование с редактированием чувствительных данных.

Правила:
- значения ключей, похожих на секреты, заменяются на `[REDACTED]`;
- значения ключей, которые могут содержать текст договора, заменяются на `[CONTENT len=N]`;
- строки, похожие на токены/ключи (Bearer, sk-…), маскируются в любых значениях;
- длинные строки обрезаются, чтобы случайно не записать текст документа целиком.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.types import EventDict, Processor

REDACTED = "[REDACTED]"
MAX_STRING_LEN = 300

_SECRET_KEY_RE = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key|session[_-]?id"
    r"|database[_-]?url|dsn)",
    re.IGNORECASE,
)
# Поля, которые могут содержать содержимое договоров или промптов.
_CONTENT_KEYS = frozenset(
    {
        "text",
        "quote",
        "content",
        "prompt",
        "messages",
        "body",
        "document_text",
        "fragment_text",
        "raw_response",
        "input",
    }
)
_SECRET_VALUE_RES = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    # пароль в URL подключения: scheme://user:password@host
    re.compile(r"(?<=://)([^:/@\s]+):([^@/\s]+)@"),
)


def _mask_secrets(value: str) -> str:
    value = _SECRET_VALUE_RES[0].sub("Bearer " + REDACTED, value)
    value = _SECRET_VALUE_RES[1].sub(REDACTED, value)
    return _SECRET_VALUE_RES[2].sub(lambda m: f"{m.group(1)}:{REDACTED}@", value)


def _mask_string(value: str) -> str:
    value = _mask_secrets(value)
    if len(value) > MAX_STRING_LEN:
        value = f"{value[:MAX_STRING_LEN]}…[truncated {len(value) - MAX_STRING_LEN} chars]"
    return value


def redact_value(key: str | None, value: Any, _depth: int = 0) -> Any:
    if _depth > 6:
        return "[DEPTH]"
    if key is not None:
        lowered = key.lower()
        if _SECRET_KEY_RE.search(lowered):
            return REDACTED
        if lowered in _CONTENT_KEYS:
            size = len(value) if isinstance(value, str | bytes | list | tuple | dict) else 0
            return f"[CONTENT len={size}]"
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, bytes):
        return f"[BYTES len={len(value)}]"
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(None, v, _depth + 1) for v in value]
    return value


def redact_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if key in {"event", "level", "timestamp", "logger"}:
            if isinstance(event_dict[key], str):
                event_dict[key] = _mask_string(event_dict[key])
            continue
        if key == "exception" and isinstance(event_dict[key], str):
            # трассировка может содержать значения из исключений: маскируем, но не режем
            event_dict[key] = _mask_secrets(event_dict[key])
            continue
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


def format_exception_safely(exc: BaseException) -> str:
    """Трассировка без текста сообщений исключений.

    Сообщение исключения может содержать данные: `input_value` ошибки валидации ответа модели
    (фрагменты договора), `DETAIL` PostgreSQL со значениями полей, фрагмент ответа провайдера.
    Для диагностики достаточно типов исключений и кадров стека (файл, строка, функция, код).
    """
    blocks: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frames = "".join(traceback.StackSummary.extract(traceback.walk_tb(current.__traceback__)).format())
        name = f"{type(current).__module__}.{type(current).__qualname__}"
        blocks.append(f"Traceback (most recent call last):\n{frames}{name}: [message hidden]")
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    # Как в стандартной трассировке: исходное исключение — первым.
    return "\n\nDuring handling, another exception occurred:\n\n".join(reversed(blocks))


def safe_exc_info_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Замена `structlog.processors.format_exc_info`, которая не выводит сообщения исключений."""
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    exc: BaseException | None
    if isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple):
        exc = exc_info[1]
    else:
        exc = sys.exc_info()[1]
    if exc is not None:
        event_dict["exception"] = format_exception_safely(exc)
    return event_dict


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _drop_color_message,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            safe_exc_info_processor,
            redact_processor,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Собственный access-лог в middleware; стандартный uvicorn.access отключаем.
    for name, lvl in (("uvicorn.access", logging.WARNING), ("sqlalchemy.engine", logging.WARNING)):
        logging.getLogger(name).setLevel(lvl)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def redact_mapping(data: MutableMapping[str, Any]) -> dict[str, Any]:
    """Публичный помощник: редактирует произвольный словарь (например, для аудита)."""
    return {k: redact_value(k, v) for k, v in data.items()}
