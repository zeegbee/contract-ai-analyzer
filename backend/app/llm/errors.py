"""Нормализованные ошибки провайдеров.

Сообщения безопасны для показа администратору: в них нет ключей, текста запросов/ответов и
сырых сообщений провайдера (те могут содержать фрагменты промпта).
"""

from __future__ import annotations


class ProviderError(Exception):
    code = "provider_error"
    message = "Ошибка провайдера модели"
    # Имеет ли смысл повторить тот же запрос (timeout/429/5xx).
    retryable = False

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or self.message
        self.code = code or self.code
        super().__init__(self.code)


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    message = "Провайдер не ответил за отведённое время"
    retryable = True


class ProviderRateLimited(ProviderError):
    code = "provider_rate_limited"
    message = "Провайдер ограничил частоту запросов (429)"
    retryable = True

    def __init__(self, retry_after: float | None = None, message: str | None = None) -> None:
        super().__init__(message)
        self.retry_after: float | None = retry_after


class ProviderUnavailable(ProviderError):
    """Сетевая ошибка или 5xx — временная недоступность."""

    code = "provider_unavailable"
    message = "Провайдер временно недоступен"
    retryable = True


class ProviderAuthError(ProviderError):
    code = "provider_auth_failed"
    message = "Провайдер отклонил ключ доступа (проверьте API-ключ)"


class ProviderQuotaExceeded(ProviderError):
    code = "provider_quota_exceeded"
    message = "Недостаточно средств или исчерпан лимит у провайдера"


class ModelUnavailable(ProviderError):
    code = "model_unavailable"
    message = "Модель недоступна у провайдера (не найдена, не загружена или отключена)"


class ContextTooLong(ProviderError):
    code = "context_too_long"
    message = "Запрос превышает контекстное окно модели"


class InvalidModelOutput(ProviderError):
    code = "invalid_model_output"
    message = "Модель вернула ответ, не соответствующий ожидаемому формату"


class ProviderRequestRejected(ProviderError):
    """400/403/422: параметры запроса не приняты (повтор не поможет)."""

    code = "provider_request_rejected"
    message = "Провайдер отклонил параметры запроса"


class ProviderNotConfigured(ProviderError):
    code = "provider_not_configured"
    message = "Провайдер не настроен (нет ключа или адреса)"


class BlockedDestination(ProviderError):
    """Адрес провайдера запрещён политикой SSRF (ARCHITECTURE §12, THREAT_MODEL §4.5)."""

    code = "destination_blocked"
    message = "Адрес провайдера запрещён политикой безопасности"
