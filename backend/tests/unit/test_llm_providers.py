"""Адаптеры OpenRouter/Ollama на mock-транспорте: формат запросов, ошибки, повторы, usage, секреты.

Live-вызовы реальных API здесь не выполняются (см. IMPLEMENTATION_STATUS, этап 05).
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest

from app.llm.base import ChatMessage, GenerationParams, StructuredRequest
from app.llm.errors import (
    BlockedDestination,
    ContextTooLong,
    InvalidModelOutput,
    ModelUnavailable,
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.llm.http import parse_retry_after
from app.llm.mock import MockProvider
from app.logging import configure_logging
from app.models.enums import ProviderType, StructuredOutputSupport
from tests.llm_helpers import (
    API_KEY,
    FakeResolver,
    Recorder,
    Sleeps,
    chat_ok,
    factory,
    or_error,
    provider_config,
    secret_box,
)

SCHEMA = {
    "type": "object",
    "properties": {"risks": {"type": "array"}},
    "required": ["risks"],
    "additionalProperties": False,
}
CONTRACT = "Арендатор уплачивает неустойку 0,5% в день. Секретное условие договора № 42."


def _request(mode: StructuredOutputSupport = StructuredOutputSupport.json_schema) -> StructuredRequest:
    return StructuredRequest(
        model="openai/gpt-test",
        messages=[ChatMessage("system", "Ты юрист."), ChatMessage("user", CONTRACT)],
        json_schema=SCHEMA,
        schema_name="chunk_analysis",
        params=GenerationParams(temperature=0.1, seed=7, max_output_tokens=1000),
        structured_output=mode,
    )


def _openrouter(recorder: Recorder, sleeps: Sleeps | None = None, **kw: object):
    box = secret_box()
    return factory(box, recorder, sleeps=sleeps).build(provider_config(box, **kw))  # type: ignore[arg-type]


def _ollama(recorder: Recorder, sleeps: Sleeps | None = None):
    box = secret_box()
    cfg = provider_config(
        box,
        ProviderType.ollama,
        base_url="http://ollama:11434",
        api_key=None,
        allow_private=True,
        is_cloud=False,
    )
    return factory(box, recorder, FakeResolver({"ollama": ["172.30.20.5"]}), sleeps).build(cfg)


# ------------------------------------------------------------------ OpenRouter


async def test_openrouter_structured_request_and_usage() -> None:
    recorder = Recorder(
        chat_ok(
            {"risks": []},
            usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.00042},
        )
    )
    result = await _openrouter(recorder).generate_structured(_request())
    assert result.parsed_json == {"risks": []}
    assert result.usage is not None
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (120, 30)
    assert result.usage.cost == Decimal("0.00042")
    assert result.provider_request_id == "gen-1"

    [request] = recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["host"] == "openrouter.ai"
    body = json.loads(request.content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "chunk_analysis", "strict": True, "schema": SCHEMA},
    }
    assert body["provider"] == {"require_parameters": True}
    assert body["stream"] is False
    assert (body["temperature"], body["seed"], body["max_tokens"]) == (0.1, 7, 1000)
    assert "tools" not in body and "tool_choice" not in body


async def test_openrouter_cost_absent_means_unknown() -> None:
    recorder = Recorder(chat_ok({"risks": []}, usage={"prompt_tokens": 10, "completion_tokens": 5}))
    result = await _openrouter(recorder).generate_structured(_request())
    assert result.usage is not None and result.usage.cost is None and result.usage.cost_currency is None
    result = await _openrouter(Recorder(chat_ok({"risks": []}))).generate_structured(_request())
    assert result.usage is None


async def test_openrouter_unconfirmed_schema_uses_instruction_without_response_format() -> None:
    recorder = Recorder(chat_ok('```json\n{"risks": []}\n```'))
    result = await _openrouter(recorder).generate_structured(_request(StructuredOutputSupport.unknown))
    assert result.parsed_json == {"risks": []}
    body = recorder.bodies()[0]
    assert "response_format" not in body
    assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]


async def test_openrouter_json_mode() -> None:
    recorder = Recorder(chat_ok({"risks": []}))
    await _openrouter(recorder).generate_structured(_request(StructuredOutputSupport.json_mode))
    assert recorder.bodies()[0]["response_format"] == {"type": "json_object"}


async def test_rate_limit_retries_with_retry_after() -> None:
    sleeps = Sleeps()
    recorder = Recorder(or_error(429, headers={"Retry-After": "2"}), chat_ok({"risks": []}))
    recorder.responses = [or_error(429, headers={"Retry-After": "2"}), chat_ok({"risks": []})]
    result = await _openrouter(recorder, sleeps).generate_structured(_request())
    assert result.parsed_json == {"risks": []}
    assert len(recorder.requests) == 2
    assert sleeps.delays and sleeps.delays[0] >= 2


async def test_rate_limit_with_too_long_retry_after_is_not_retried() -> None:
    sleeps = Sleeps()
    recorder = Recorder(or_error(429, headers={"Retry-After": "3600"}))
    with pytest.raises(ProviderRateLimited) as exc:
        await _openrouter(recorder, sleeps).generate_structured(_request())
    assert exc.value.retry_after == 3600
    assert len(recorder.requests) == 1 and sleeps.delays == []


async def test_server_errors_retried_limited_times() -> None:
    sleeps = Sleeps()
    recorder = Recorder(or_error(503))
    with pytest.raises(ProviderUnavailable):
        await _openrouter(recorder, sleeps).generate_structured(_request())
    assert len(recorder.requests) == 4  # 1 + 3 повтора
    assert len(sleeps.delays) == 3
    assert sleeps.delays[0] < sleeps.delays[2] or sleeps.delays[2] == pytest.approx(0.05, abs=0.03)


async def test_timeout_is_normalized_and_retried() -> None:
    recorder = Recorder(httpx.ReadTimeout("read timed out"))
    with pytest.raises(ProviderTimeout):
        await _openrouter(recorder).generate_structured(_request())
    assert len(recorder.requests) == 4


async def test_connect_error_is_unavailable_without_details() -> None:
    recorder = Recorder(httpx.ConnectError("[Errno 111] connection refused to 93.184.216.34"))
    with pytest.raises(ProviderUnavailable) as exc:
        await _openrouter(recorder).generate_structured(_request())
    assert exc.value.code == "connection_failed"
    assert "93.184" not in exc.value.message


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (or_error(401, "Invalid API key sk-or-v1-0123"), ProviderAuthError),
        (or_error(402, "Insufficient credits"), ProviderQuotaExceeded),
        (or_error(404, "No endpoints found matching your data policy"), ModelUnavailable),
        (or_error(400, "openai/gpt-x is not a valid model ID"), ModelUnavailable),
        (or_error(400, "This endpoint's maximum context length is 8192 tokens"), ContextTooLong),
    ],
)
async def test_non_retryable_errors(response: httpx.Response, error: type[Exception]) -> None:
    recorder = Recorder(response)
    with pytest.raises(error) as exc:
        await _openrouter(recorder).generate_structured(_request())
    assert len(recorder.requests) == 1
    # Сырой текст провайдера наружу не передаётся.
    assert "sk-or" not in str(exc.value.message) and "data policy" not in str(exc.value.message)  # type: ignore[attr-defined]


async def test_error_in_body_with_http_200() -> None:
    recorder = Recorder(httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}))
    with pytest.raises(ProviderUnavailable):
        await _openrouter(recorder).generate_structured(_request())
    assert len(recorder.requests) == 4
    recorder = Recorder(chat_ok("partial", finish="error"))
    with pytest.raises(ProviderUnavailable):
        await _openrouter(recorder).generate_structured(_request())


async def test_invalid_or_truncated_output_is_not_retried() -> None:
    recorder = Recorder(chat_ok("Вот ваш ответ: {risks: []}"))
    with pytest.raises(InvalidModelOutput) as exc:
        await _openrouter(recorder).generate_structured(_request())
    assert exc.value.code == "invalid_json" and len(recorder.requests) == 1
    with pytest.raises(InvalidModelOutput) as exc:
        await _openrouter(Recorder(chat_ok('{"risks": [', finish="length"))).generate_structured(_request())
    assert exc.value.code == "output_truncated"
    with pytest.raises(InvalidModelOutput):
        await _openrouter(Recorder(chat_ok("[1, 2]"))).generate_structured(_request())


async def test_response_size_limit() -> None:
    recorder = Recorder(httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 10)))
    with pytest.raises(ProviderUnavailable) as exc:
        await _openrouter(recorder).generate_structured(_request())
    assert exc.value.code == "response_too_large"


async def test_openrouter_without_key_is_not_configured() -> None:
    box = secret_box()
    with pytest.raises(ProviderNotConfigured):
        factory(box, Recorder(chat_ok({}))).build(provider_config(box, api_key=None))


async def test_blocked_base_url_never_reaches_network() -> None:
    recorder = Recorder(chat_ok({"risks": []}))
    with pytest.raises(BlockedDestination):
        await _openrouter(recorder, base_url="https://169.254.169.254/v1").generate_structured(_request())
    assert recorder.requests == []


async def test_check_connection_and_auth_failure() -> None:
    ok = await _openrouter(
        Recorder(
            httpx.Response(
                200, json={"data": {"label": "x", "limit": None, "usage": 1.5, "is_free_tier": False}}
            )
        )
    ).check_connection()
    assert ok.ok and ok.details == {"limit": None, "usage": 1.5, "is_free_tier": False}
    recorder = Recorder(or_error(401))
    with pytest.raises(ProviderAuthError):
        await _openrouter(recorder).check_connection()
    assert recorder.requests[0].url.path == "/api/v1/key"


async def test_list_models_converts_prices_per_million_and_keeps_unknown() -> None:
    models = {
        "data": [
            {
                "id": "openai/gpt-test",
                "name": "GPT Test",
                "context_length": 128000,
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                "top_provider": {"max_completion_tokens": 16384, "is_moderated": True},
                "supported_parameters": ["temperature", "structured_outputs", "response_format"],
            },
            {"id": "openrouter/auto", "name": "Auto", "pricing": {"prompt": "-1", "completion": "-1"}},
            {"id": "x/no-pricing", "name": "No pricing", "context_length": None},
            "garbage",
        ]
    }
    [first, auto, bare] = await _openrouter(Recorder(httpx.Response(200, json=models))).list_models()
    assert first.prompt_price_per_mtok == Decimal("2.5")
    assert first.completion_price_per_mtok == Decimal("10")
    assert (first.context_window, first.max_output_tokens) == (128000, 16384)
    assert first.advertises_structured_output is True
    assert auto.prompt_price_per_mtok is None and auto.completion_price_per_mtok is None
    assert bare.context_window is None and bare.prompt_price_per_mtok is None


def test_retry_after_parsing() -> None:
    assert parse_retry_after("5") == 5
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


async def test_api_key_never_in_logs_errors_or_repr(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger("httpx").setLevel(logging.INFO)
    provider = _openrouter(Recorder(or_error(401, f"Invalid key {API_KEY}")))
    with pytest.raises(ProviderAuthError) as exc:
        await provider.generate_structured(_request())
    out = capsys.readouterr().out + capsys.readouterr().err
    secret_tail = API_KEY[10:]
    assert secret_tail not in out
    assert CONTRACT not in out
    assert "llm_http" in out
    assert secret_tail not in str(exc.value) and secret_tail not in repr(exc.value)
    assert secret_tail not in repr(provider) and secret_tail not in repr(provider.inner.http)


# ------------------------------------------------------------------ Ollama


async def test_ollama_chat_with_schema_and_usage() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": '{"risks": []}'},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 50,
                "eval_count": 7,
            },
        )
    )
    result = await _ollama(recorder).generate_structured(_request())
    assert result.parsed_json == {"risks": []}
    assert result.usage is not None and result.usage.total_tokens == 57 and result.usage.cost is None
    [request] = recorder.requests
    assert request.url.path == "/api/chat" and request.url.host == "172.30.20.5"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["format"] == SCHEMA and body["stream"] is False
    assert body["options"] == {"temperature": 0.1, "seed": 7, "num_predict": 1000}


async def test_ollama_model_not_pulled() -> None:
    recorder = Recorder(httpx.Response(404, json={"error": "model 'qwen3:8b' not found"}))
    with pytest.raises(ModelUnavailable) as exc:
        await _ollama(recorder).generate_structured(_request())
    assert "ollama pull" in exc.value.message
    assert len(recorder.requests) == 1
    assert all(r.url.path != "/api/pull" for r in recorder.requests)


async def test_ollama_timeout_and_health() -> None:
    with pytest.raises(ProviderTimeout):
        await _ollama(Recorder(httpx.ReadTimeout("slow"))).generate_structured(_request())
    health = await _ollama(Recorder(httpx.Response(200, json={"version": "0.12.6"}))).check_connection()
    assert health.ok and health.details == {"version": "0.12.6"}
    with pytest.raises(ProviderUnavailable) as exc:
        await _ollama(Recorder(httpx.Response(404, text="not found"))).check_connection()
    assert exc.value.code == "invalid_provider_response"


async def test_ollama_describe_and_list() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={"capabilities": ["completion", "thinking"], "model_info": {"qwen3.context_length": 40960}},
        )
    )
    info = await _ollama(recorder).describe_model("qwen3:8b")
    assert info.context_window == 40960 and info.prompt_price_per_mtok is None
    assert json.loads(recorder.requests[0].content) == {"model": "qwen3:8b"}
    listing = await _ollama(
        Recorder(
            httpx.Response(
                200, json={"models": [{"name": "qwen3:8b", "details": {"parameter_size": "8.2B"}}]}
            )
        )
    ).list_models()
    assert [m.external_model_id for m in listing] == ["qwen3:8b"]


async def test_local_ollama_cannot_reach_public_address() -> None:
    box = secret_box()
    cfg = provider_config(
        box,
        ProviderType.ollama,
        base_url="http://ollama.lan:11434",
        api_key=None,
        allow_private=True,
        is_cloud=False,
    )
    recorder = Recorder(httpx.Response(200, json={"version": "1"}))
    provider = factory(box, recorder, FakeResolver({"ollama.lan": ["8.8.8.8"]})).build(cfg)
    with pytest.raises(BlockedDestination) as exc:
        await provider.check_connection()
    assert exc.value.code == "public_address_blocked"


# ------------------------------------------------------------------ mock


async def test_mock_provider_scenarios() -> None:
    mock = MockProvider(scenarios=[ProviderTimeout(), {"risks": []}])
    with pytest.raises(ProviderTimeout):
        await mock.generate_structured(_request())
    assert (await mock.generate_structured(_request())).parsed_json == {"risks": []}
    assert mock.calls[0] == {"model": "openai/gpt-test", "messages": 2, "schema_name": "chunk_analysis"}
