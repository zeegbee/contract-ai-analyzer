"""Создание провайдера по записи каталога (`ProviderConfig`).

Секрет расшифровывается только здесь и сразу попадает в заголовок HTTP-клиента; в объекты
результата, логи и snapshot он не передаётся.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.llm.base import LLMProvider, ProviderHealth, RemoteModelInfo, StructuredRequest, StructuredResult
from app.llm.errors import ProviderNotConfigured
from app.llm.http import ProviderHttp, RetryPolicy
from app.llm.netguard import NetworkPolicy, Resolver
from app.llm.ollama import OllamaProvider
from app.llm.openrouter import OpenRouterProvider
from app.models.catalog import ProviderConfig
from app.models.enums import ProviderType
from app.security.secretbox import SecretBox


def network_policy(cfg: ProviderConfig) -> NetworkPolicy:
    """Политика адресов провайдера.

    - `allow_private_network` (только Superadmin) — разрешает частную сеть и http;
    - локальный провайдер (`is_cloud=false`, возможен только при частной сети) — публичные адреса
      запрещены, чтобы текст договора не ушёл в интернет при смене DNS/адреса.
    """
    return NetworkPolicy(
        allow_private=cfg.allow_private_network,
        allow_public=cfg.is_cloud,
        allow_http=cfg.allow_private_network,
    )


class _Throttled:
    """Ограничение параллельных генераций на провайдера (`max_concurrency`) в пределах процесса."""

    def __init__(self, inner: LLMProvider, semaphore: asyncio.Semaphore) -> None:
        self.inner = inner
        self.semaphore = semaphore

    async def generate_structured(self, req: StructuredRequest) -> StructuredResult:
        async with self.semaphore:
            return await self.inner.generate_structured(req)

    async def check_connection(self) -> ProviderHealth:
        return await self.inner.check_connection()

    async def list_models(self) -> list[RemoteModelInfo]:
        return await self.inner.list_models()

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        return await self.inner.describe_model(external_model_id)


class ProviderFactory:
    def __init__(
        self,
        secret_box: SecretBox,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        override: Callable[[ProviderConfig], LLMProvider] | None = None,
    ) -> None:
        self.secret_box = secret_box
        # transport/resolver/sleep подменяются в тестах; SSRF-guard остаётся в цепочке всегда.
        self._transport = transport
        self._resolver = resolver
        self._retry = retry
        self._sleep = sleep
        self._override = override
        self._semaphores: dict[tuple[uuid.UUID, int], asyncio.Semaphore] = {}

    def _semaphore(self, cfg: ProviderConfig) -> asyncio.Semaphore:
        key = (cfg.id, cfg.max_concurrency)
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(max(1, cfg.max_concurrency))
        return self._semaphores[key]

    def _secret(self, cfg: ProviderConfig) -> str | None:
        if cfg.secret_ciphertext is None:
            return None
        return self.secret_box.decrypt(cfg.secret_ciphertext)

    def build(self, cfg: ProviderConfig) -> LLMProvider:
        if self._override is not None:
            return _Throttled(self._override(cfg), self._semaphore(cfg))
        headers: dict[str, str] = {str(k): str(v) for k, v in (cfg.extra_headers or {}).items()}
        secret = self._secret(cfg)
        if cfg.type is ProviderType.openrouter and not secret:
            raise ProviderNotConfigured("Для OpenRouter не задан API-ключ", code="api_key_missing")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        http = ProviderHttp(
            provider=cfg.type.value,
            base_url=cfg.base_url,
            policy=network_policy(cfg),
            timeout_s=cfg.timeout_s,
            headers=headers,
            transport=self._transport,
            resolver=self._resolver,
            retry=self._retry,
            **kwargs,
        )
        inner: LLMProvider = (
            OpenRouterProvider(http) if cfg.type is ProviderType.openrouter else OllamaProvider(http)
        )
        return _Throttled(inner, self._semaphore(cfg))
