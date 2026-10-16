"""HTTP-клиент провайдеров: SSRF-guard, раздельные таймауты, лимит ответа, ограниченные повторы.

Логируются только метаданные (провайдер, путь, статус, длительность, попытка) — без заголовков
(там ключ) и тел запросов/ответов (там текст договора).
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx

from app.llm.errors import (
    ContextTooLong,
    ModelUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderRequestRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.llm.netguard import GuardedTransport, NetworkPolicy, Resolver
from app.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class RetryPolicy:
    # Повторы сверх первой попытки — только для timeout/429/5xx.
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    # Если провайдер просит ждать дольше — не ждём, а возвращаем ошибку (задача повторится позже).
    max_retry_after: float = 60.0


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = moment.timestamp() - time.time()
    return None if math.isnan(seconds) else max(0.0, seconds)


def map_status(raw: RawResponse) -> ProviderError:
    """Общая нормализация HTTP-статуса ошибки (адаптеры уточняют частные случаи)."""
    status = raw.status
    if status == 401:
        return ProviderAuthError()
    if status == 402:
        return ProviderQuotaExceeded()
    if status == 403:
        return ProviderRequestRejected(
            "Провайдер отказал в доступе (права ключа, модерация или ограничения модели)",
            code="provider_forbidden",
        )
    if status == 404:
        return ModelUnavailable()
    if status == 408:
        return ProviderTimeout()
    if status == 413:
        return ContextTooLong()
    if status == 429:
        return ProviderRateLimited(parse_retry_after(raw.headers.get("retry-after")))
    if status >= 500:
        return ProviderUnavailable(code="provider_unavailable")
    return ProviderRequestRejected()


class ProviderHttp:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        policy: NetworkPolicy,
        timeout_s: float,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.timeout_s = timeout_s
        self._headers = headers or {}
        self._transport = transport
        self._resolver = resolver
        self.retry = retry or RetryPolicy()
        self._sleep = sleep

    def __repr__(self) -> str:  # заголовки содержат ключ — не выводим
        return f"ProviderHttp(provider={self.provider!r})"

    async def send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout_s: float | None = None,
        max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> RawResponse:
        read_timeout = timeout_s or self.timeout_s
        timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_SECONDS, write=30.0, pool=10.0)
        transport = GuardedTransport(self.policy, inner=self._transport, resolver=self._resolver)
        started = time.monotonic()
        status: int | str = "error"
        try:
            # Общий предел: защищает от «медленной струйки», которую не ловит read-timeout.
            async with (
                asyncio.timeout(read_timeout + CONNECT_TIMEOUT_SECONDS + 5),
                httpx.AsyncClient(
                    transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    method, f"{self.base_url}{path}", json=json_body, headers=self._headers
                ) as response,
            ):
                status = response.status_code
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ProviderUnavailable(
                            "Ответ провайдера превышает допустимый размер", code="response_too_large"
                        )
                return RawResponse(status=response.status_code, headers=response.headers, body=bytes(body))
        except TimeoutError:
            status = "timeout"
            raise ProviderTimeout() from None
        except httpx.TimeoutException:
            status = "timeout"
            raise ProviderTimeout() from None
        except httpx.TransportError:
            # Текст исключения может содержать адрес — наружу отдаём только код.
            raise ProviderUnavailable(
                "Не удалось подключиться к провайдеру", code="connection_failed"
            ) from None
        finally:
            log.info(
                "llm_http",
                provider=self.provider,
                path=path,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _delay(self, attempt: int, error: ProviderError) -> float | None:
        """Пауза перед повтором или None — не повторять."""
        if not error.retryable or attempt >= self.retry.max_retries:
            return None
        backoff = min(self.retry.max_delay, self.retry.base_delay * (2.0**attempt))
        delay = backoff * (0.5 + random.random() / 2)  # noqa: S311 - jitter, не криптография
        if isinstance(error, ProviderRateLimited) and error.retry_after is not None:
            if error.retry_after > self.retry.max_retry_after:
                return None
            delay = max(delay, error.retry_after)
        return delay

    async def with_retries(self, operation: Callable[[], Awaitable[T]], *, op: str) -> T:
        attempt = 0
        while True:
            try:
                return await operation()
            except ProviderError as error:
                delay = self._delay(attempt, error)
                if delay is None:
                    raise
                attempt += 1
                log.warning(
                    "llm_retry",
                    provider=self.provider,
                    op=op,
                    error_code=error.code,
                    attempt=attempt,
                    delay_ms=int(delay * 1000),
                )
                await self._sleep(delay)
