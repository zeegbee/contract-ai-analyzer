"""Общие помощники тестов провайдеров: подставной DNS, транспорт, конфигурации."""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.llm.factory import ProviderFactory
from app.llm.http import RetryPolicy
from app.llm.netguard import IPAddress
from app.models.catalog import ProviderConfig
from app.models.enums import ProviderType
from app.security.secretbox import SecretBox

PUBLIC_IP = "93.184.216.34"
API_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


class FakeResolver:
    """DNS для тестов: имя → список адресов; можно задать последовательность ответов (rebinding)."""

    def __init__(self, mapping: dict[str, list[str] | list[list[str]]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.calls.append(host)
        value = self.mapping.get(host, [PUBLIC_IP])
        if value and isinstance(value[0], list):
            answers: list[str] = value.pop(0) if len(value) > 1 else value[0]
        else:
            answers = value  # type: ignore[assignment]
        return [ipaddress.ip_address(a) for a in answers]


class Recorder:
    """MockTransport-обработчик: очередь ответов + запись запросов."""

    def __init__(self, *responses: httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]


def chat_ok(content: Any, *, usage: dict[str, Any] | None = None, finish: str = "stop") -> httpx.Response:
    body: dict[str, Any] = {
        "id": "gen-1",
        "model": "openai/gpt-test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else json.dumps(content),
                },
                "finish_reason": finish,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def or_error(status: int, message: str = "error", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}}, headers=headers)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def secret_box() -> SecretBox:
    return SecretBox([SecretStr(Fernet.generate_key().decode())])


def provider_config(
    box: SecretBox,
    type_: ProviderType = ProviderType.openrouter,
    *,
    base_url: str = "https://openrouter.ai/api/v1",
    api_key: str | None = API_KEY,
    allow_private: bool = False,
    is_cloud: bool = True,
    max_concurrency: int = 2,
) -> ProviderConfig:
    cfg = ProviderConfig(
        id=uuid.uuid4(),
        name="test",
        type=type_,
        base_url=base_url,
        is_cloud=is_cloud,
        enabled=True,
        allow_private_network=allow_private,
        timeout_s=30,
        max_concurrency=max_concurrency,
        extra_headers={},
        version=1,
    )
    if api_key:
        cfg.secret_ciphertext, cfg.secret_key_id = box.encrypt(api_key)
        cfg.secret_last4 = api_key[-4:]
    return cfg


def factory(
    box: SecretBox, recorder: Recorder, resolver: FakeResolver | None = None, sleeps: Sleeps | None = None
) -> ProviderFactory:
    return ProviderFactory(
        box,
        transport=httpx.MockTransport(recorder),
        resolver=resolver or FakeResolver(),
        retry=RetryPolicy(max_retries=3, base_delay=0.01, max_delay=0.05, max_retry_after=60),
        sleep=sleeps or Sleeps(),
    )
