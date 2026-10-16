"""Каталог провайдеров и моделей через API: права, секреты, SSRF, проверка, импорт, доступность, согласие.

Сеть провайдеров подменена `httpx.MockTransport` (SSRF-guard остаётся в цепочке); live-вызовы
OpenRouter/Ollama не выполняются.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from app.errors import AppError
from app.llm.factory import ProviderFactory
from app.llm.http import RetryPolicy
from app.models.enums import UserRole
from app.models.users import User
from app.services import catalog as svc
from tests.integration.conftest import ApiClient, ApiEnv
from tests.llm_helpers import API_KEY, FakeResolver, Recorder, Sleeps, chat_ok, or_error

pytestmark = pytest.mark.integration


def use_network(api: ApiEnv, recorder: Recorder, resolver: FakeResolver | None = None) -> None:
    api.app.state.llm_factory = ProviderFactory(
        api.app.state.secret_box,
        transport=httpx.MockTransport(recorder),
        resolver=resolver or FakeResolver({"ollama": ["172.30.20.5"]}),
        retry=RetryPolicy(max_retries=1, base_delay=0.01, max_delay=0.01),
        sleep=Sleeps(),
    )


async def _superadmin(api: ApiEnv) -> ApiClient:
    return await api.login(await api.create_user(UserRole.superadmin))


async def _openrouter(c: ApiClient, **extra: Any) -> dict[str, Any]:
    body = {
        "name": f"OpenRouter {uuid.uuid4().hex[:6]}",
        "type": "openrouter",
        "api_key": API_KEY,
        "enabled": True,
    }
    r = await c.post("/api/admin/providers", json={**body, **extra})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def _ollama(c: ApiClient, **extra: Any) -> dict[str, Any]:
    body = {
        "name": f"Ollama {uuid.uuid4().hex[:6]}",
        "type": "ollama",
        "base_url": "http://ollama:11434",
        "allow_private_network": True,
        "enabled": True,
    }
    r = await c.post("/api/admin/providers", json={**body, **extra})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def _model(c: ApiClient, provider: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body = {
        "provider_config_id": provider["id"],
        "external_model_id": f"vendor/model-{uuid.uuid4().hex[:6]}",
        "display_name": "Модель",
        "enabled": True,
    }
    r = await c.post("/api/admin/models", json={**body, **extra})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


# ------------------------------------------------------------------ провайдеры и секреты


async def test_secret_is_encrypted_and_never_returned(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
        assert provider["has_secret"] is True and provider["secret_last4"] == API_KEY[-4:]
        assert provider["is_cloud"] is True and provider["base_url"] == "https://openrouter.ai/api/v1"
        listing = await c.get("/api/admin/providers")
        one = await c.get(f"/api/admin/providers/{provider['id']}")
        revisions = await c.get("/api/admin/config-revisions", params={"entity_type": "provider"})
        for response in (listing, one, revisions):
            assert response.status_code == 200
            assert API_KEY not in response.text and API_KEY[:-4] not in response.text
    stored = await api.scalar(
        "SELECT secret_ciphertext FROM provider_configs WHERE id = :i", i=provider["id"]
    )
    assert API_KEY.encode() not in bytes(stored)
    for (details,) in await api.rows("SELECT details FROM audit_log"):
        assert API_KEY not in json.dumps(details)
    [(diff,)] = await api.rows("SELECT diff FROM config_revisions WHERE entity_type = 'provider'")
    assert diff["api_key"] == {"from": None, "to": "***"}


async def test_changing_base_url_resets_secret(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c, enabled=False)
        r = await c.patch(
            f"/api/admin/providers/{provider['id']}",
            json={"version": provider["version"], "base_url": "https://proxy.example.com/api/v1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["has_secret"] is False and r.headers.get("x-secret-reset") == "1"
        # Включить OpenRouter без ключа нельзя.
        r = await c.patch(
            f"/api/admin/providers/{provider['id']}", json={"version": r.json()["version"], "enabled": True}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "api_key_missing"


async def test_version_conflict(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
        url = f"/api/admin/providers/{provider['id']}"
        assert (await c.patch(url, json={"version": provider["version"], "timeout_s": 60})).status_code == 200
        r = await c.patch(url, json={"version": provider["version"], "timeout_s": 90})
        assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"type": "openrouter", "base_url": "https://169.254.169.254/latest"}, "metadata_blocked"),
        ({"type": "openrouter", "base_url": "https://10.0.0.5/v1"}, "private_address_blocked"),
        ({"type": "openrouter", "base_url": "https://[::1]/v1"}, "loopback_blocked"),
        ({"type": "openrouter", "base_url": "http://openrouter.ai/api/v1"}, "https_required"),
        ({"type": "openrouter", "base_url": "https://u:p@openrouter.ai/api/v1"}, "userinfo_not_allowed"),
        ({"type": "openrouter", "base_url": "file:///etc/passwd"}, "scheme_not_allowed"),
        ({"type": "openrouter", "allow_private_network": True}, "private_network_not_allowed"),
        ({"type": "ollama", "base_url": "http://ollama:11434"}, "https_required"),
        ({"type": "ollama", "base_url": "https://127.0.0.1:11434"}, "loopback_blocked"),
        (
            {"type": "ollama", "base_url": "https://ollama.example.com", "is_cloud": False},
            "local_requires_private_network",
        ),
        (
            {"type": "ollama", "base_url": "http://169.254.169.254", "allow_private_network": True},
            "metadata_blocked",
        ),
        (
            {"type": "ollama", "extra_headers": {"X-Api-Key": "secret"}, "allow_private_network": True},
            "secret_in_header",
        ),
        (
            {"type": "ollama", "extra_headers": {"Host": "evil"}, "allow_private_network": True},
            "invalid_header",
        ),
    ],
)
async def test_provider_url_validation(api: ApiEnv, body: dict[str, Any], code: str) -> None:
    c = await _superadmin(api)
    async with c:
        r = await c.post("/api/admin/providers", json={"name": "P", **body})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code
    assert await api.scalar("SELECT count(*) FROM provider_configs") == 0


async def test_private_ollama_is_local_and_check_uses_network_guard(api: ApiEnv) -> None:
    recorder = Recorder(httpx.Response(200, json={"version": "0.12.6"}))
    use_network(api, recorder)
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        assert provider["is_cloud"] is False and provider["has_secret"] is False
        r = await c.post(f"/api/admin/providers/{provider['id']}/check")
        assert r.status_code == 200, r.text
        assert r.json()["health"] == {
            "ok": True,
            "status": "ok",
            "message": "Ollama доступна",
            "details": {"version": "0.12.6"},
        }
        assert r.json()["provider"]["last_check_status"] == "ok"
        # Проверка не меняет версию конфигурации.
        assert r.json()["provider"]["version"] == provider["version"]
    assert recorder.requests[0].url.host == "172.30.20.5"
    assert recorder.requests[0].headers["host"] == "ollama:11434"


async def test_local_ollama_resolving_to_public_address_is_blocked(api: ApiEnv) -> None:
    recorder = Recorder(httpx.Response(200, json={"version": "1"}))
    use_network(api, recorder, FakeResolver({"ollama": ["8.8.8.8"]}))
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        r = await c.post(f"/api/admin/providers/{provider['id']}/check")
    assert r.json()["health"]["ok"] is False
    assert r.json()["health"]["status"] == "public_address_blocked"
    assert recorder.requests == []


async def test_check_with_wrong_key_reports_auth_error(api: ApiEnv) -> None:
    use_network(api, Recorder(or_error(401, f"bad key {API_KEY}")))
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
        r = await c.post(f"/api/admin/providers/{provider['id']}/check")
    body = r.json()
    assert body["health"]["ok"] is False and body["health"]["status"] == "provider_auth_failed"
    assert body["provider"]["last_check_status"] == "provider_auth_failed"
    assert API_KEY not in r.text


async def test_role_matrix_for_catalog(api: ApiEnv) -> None:
    sa = await _superadmin(api)
    async with sa:
        provider = await _openrouter(sa)
        model = await _model(sa, provider)
    admin = await api.login(await api.create_user(UserRole.admin))
    async with admin:
        assert (await admin.get("/api/admin/providers")).status_code == 200
        assert (await admin.get("/api/admin/models")).status_code == 200
        # Провайдеры и глобальные настройки — только Superadmin.
        assert (
            await admin.post("/api/admin/providers", json={"name": "X", "type": "openrouter"})
        ).status_code == 403
        r = await admin.patch(f"/api/admin/providers/{provider['id']}", json={"version": 1, "enabled": False})
        assert r.status_code == 403
        assert (await admin.delete(f"/api/admin/providers/{provider['id']}")).status_code == 403
        assert (
            await admin.put("/api/admin/llm-settings", json={"cloud_models_enabled": True})
        ).status_code == 403
        assert (await admin.get("/api/admin/llm-settings")).json()["can_edit"] is False
        # Модели — Admin может.
        r = await admin.patch(
            f"/api/admin/models/{model['id']}", json={"version": model["version"], "priority": 5}
        )
        assert r.status_code == 200, r.text
    for role in (UserRole.analyst, UserRole.viewer):
        c = await api.login(await api.create_user(role))
        async with c:
            for response in (
                await c.get("/api/admin/providers"),
                await c.get("/api/admin/models"),
                await c.get(f"/api/admin/models/{model['id']}"),
                await c.post(f"/api/admin/models/{model['id']}/test", json={}),
                await c.post("/api/admin/models", json={}),
                await c.get("/api/admin/config-revisions"),
            ):
                assert response.status_code == 403, response.text


# ------------------------------------------------------------------ модели


async def test_single_default_model_and_disable_clears_default(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        first = await _model(c, provider, is_default=True)
        second = await _model(c, provider, is_default=True)
        models = {m["id"]: m for m in (await c.get("/api/admin/models")).json()["items"]}
        assert models[first["id"]]["is_default"] is False and models[second["id"]]["is_default"] is True
        r = await c.patch(
            f"/api/admin/models/{second['id']}",
            json={"version": models[second["id"]]["version"], "enabled": False},
        )
        assert r.json()["is_default"] is False
        r = await c.post(
            "/api/admin/models",
            json={
                "provider_config_id": provider["id"],
                "external_model_id": "x",
                "display_name": "x",
                "is_default": True,
            },
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_default_model"


async def test_manual_json_schema_flag_is_rejected_and_pricing_marked_manual(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        r = await c.post(
            "/api/admin/models",
            json={
                "provider_config_id": provider["id"],
                "external_model_id": "m",
                "display_name": "m",
                "structured_output": "json_schema",
            },
        )
        assert r.status_code == 422
        model = await _model(c, provider, pricing={"prompt_per_mtok": "1.5", "completion_per_mtok": "2"})
        assert model["pricing"]["source"] == "admin_manual" and model["pricing"]["prompt_per_mtok"] == "1.5"
        unknown = await _model(c, provider)
        assert unknown["pricing"] is None


async def test_model_test_confirms_structured_output_and_fills_metadata(api: ApiEnv) -> None:
    models_body = {
        "data": [
            {
                "id": "openai/gpt-test",
                "name": "GPT Test",
                "context_length": 128000,
                "pricing": {"prompt": "0.000002", "completion": "0.000008"},
                "top_provider": {"max_completion_tokens": 16000, "is_moderated": False},
                "supported_parameters": ["structured_outputs"],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=models_body)
        return chat_ok({"status": "ok"}, usage={"prompt_tokens": 20, "completion_tokens": 5})

    recorder = Recorder(handler)
    use_network(api, recorder)
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
        model = await _model(c, provider, external_model_id="openai/gpt-test")
        assert model["structured_output"] == "unknown"
        r = await c.post(f"/api/admin/models/{model['id']}/test", json={"generate": True})
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["ok"] is True
    assert [s["name"] for s in body["steps"]] == ["describe", "generate"]
    tested = body["model"]
    assert tested["structured_output"] == "json_schema"
    assert tested["context_window"] == 128000 and tested["max_output_tokens"] == 16000
    assert tested["pricing"]["source"] == "provider_api" and tested["pricing"]["prompt_per_mtok"] == "2"
    assert tested["last_test_status"] == "ok"
    chat = [json.loads(r.content) for r in recorder.requests if r.url.path.endswith("/chat/completions")]
    assert chat[0]["response_format"]["type"] == "json_schema"


async def test_model_test_unavailable_model(api: ApiEnv) -> None:
    use_network(api, Recorder(httpx.Response(404, json={"error": "model 'x' not found"})))
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        model = await _model(c, provider)
        body = (await c.post(f"/api/admin/models/{model['id']}/test", json={"generate": True})).json()
    assert body["ok"] is False
    assert body["model"]["last_test_status"] == "model_unavailable"
    assert "ollama pull" in body["model"]["last_test_error_safe"]
    assert body["model"]["structured_output"] == "unknown"


async def test_import_models_from_provider(api: ApiEnv) -> None:
    models_body = {
        "data": [
            {
                "id": "a/one",
                "name": "One",
                "context_length": 8192,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {"id": "b/two", "name": "Two", "pricing": {"prompt": "-1", "completion": "-1"}},
        ]
    }
    use_network(api, Recorder(httpx.Response(200, json=models_body)))
    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
        listing = (
            await c.get(f"/api/admin/providers/{provider['id']}/remote-models", params={"q": "one"})
        ).json()
        assert [m["external_model_id"] for m in listing["items"]] == ["a/one"]
        r = await c.post(
            f"/api/admin/providers/{provider['id']}/import-models",
            json={"external_model_ids": ["a/one", "b/two"]},
        )
        assert r.status_code == 201, r.text
        imported = {m["external_model_id"]: m for m in r.json()["items"]}
        assert imported["a/one"]["enabled"] is False and imported["a/one"]["context_window"] == 8192
        assert imported["a/one"]["pricing"]["source"] == "provider_api"
        assert imported["b/two"]["pricing"] is None  # цена неизвестна — не выдумывается
        listing = (await c.get(f"/api/admin/providers/{provider['id']}/remote-models")).json()
        assert all(m["already_added"] for m in listing["items"])
        r = await c.post(
            f"/api/admin/providers/{provider['id']}/import-models", json={"external_model_ids": ["zzz/none"]}
        )
        assert r.status_code == 422


async def test_provider_with_models_cannot_be_deleted(api: ApiEnv) -> None:
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        model = await _model(c, provider)
        r = await c.delete(f"/api/admin/providers/{provider['id']}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "provider_has_models"
        assert (await c.delete(f"/api/admin/models/{model['id']}")).status_code == 204
        assert (await c.delete(f"/api/admin/providers/{provider['id']}")).status_code == 204


# ------------------------------------------------------------------ доступность, облако, согласие, snapshot


async def _available(c: ApiClient) -> dict[str, Any]:
    r = await c.get("/api/models/available")
    assert r.status_code == 200
    data: dict[str, Any] = r.json()
    return data


async def test_available_models_respect_roles_overrides_and_disabled(api: ApiEnv) -> None:
    sa = await _superadmin(api)
    analyst = await api.create_user(UserRole.analyst)
    async with sa:
        local = await _ollama(sa)
        visible = await _model(sa, local, display_name="Видимая")
        admins_only = await _model(
            sa, local, display_name="Только админы", allowed_roles=["admin", "superadmin"]
        )
        disabled = await _model(sa, local, display_name="Выключена", enabled=False)
        verifier_only = await _model(sa, local, display_name="Верификатор", purpose=["verifier"])
        disabled_provider = await _ollama(sa, enabled=False)
        await _model(sa, disabled_provider, display_name="Провайдер выключен")
        r = await sa.put(
            f"/api/users/{analyst.id}/model-permissions",
            json={"overrides": [{"model_config_id": admins_only["id"], "allowed": True}]},
        )
        assert r.status_code == 200, r.text
    c = await api.login(analyst)
    async with c:
        names = [m["display_name"] for m in (await _available(c))["items"]]
        assert sorted(names) == ["Видимая", "Только админы"]
        verifiers = (await c.get("/api/models/available", params={"purpose": "verifier"})).json()["items"]
        assert [m["id"] for m in verifiers] == [verifier_only["id"]]
    viewer = await api.login(await api.create_user(UserRole.viewer))
    async with viewer:
        assert (await _available(viewer))["items"] == []
    # Персональный запрет сильнее роли; серверная проверка совпадает со списком.
    async with sa:
        await sa.put(
            f"/api/users/{analyst.id}/model-permissions",
            json={"overrides": [{"model_config_id": visible["id"], "allowed": False}]},
        )
    async with api.factory() as db:
        user = await db.get(User, analyst.id)
        assert user is not None
        for model_id, code in (
            (visible["id"], "model_not_found"),
            (admins_only["id"], "model_not_found"),
            (disabled["id"], "model_disabled"),
            (str(uuid.uuid4()), "model_not_found"),
        ):
            with pytest.raises(AppError) as exc:
                await svc.resolve_model_for_use(db, user, uuid.UUID(model_id), cloud_consent=False)
            assert exc.value.code == code


async def test_cloud_switch_consent_and_snapshot(api: ApiEnv) -> None:
    sa = await _superadmin(api)
    analyst = await api.create_user(UserRole.analyst)
    async with sa:
        cloud = await _openrouter(sa)
        cloud_model = await _model(sa, cloud, display_name="Облачная")
        settings = (await sa.get("/api/admin/llm-settings")).json()
        assert settings["cloud_models_enabled"] is False  # по умолчанию облако выключено
    c = await api.login(analyst)
    async with c:
        available = await _available(c)
        assert available["items"] == [] and available["cloud_models_enabled"] is False
    async with sa:
        r = await sa.put("/api/admin/llm-settings", json={"cloud_models_enabled": True})
        assert r.status_code == 200 and r.json()["cloud_models_enabled"] is True
    async with c:
        available = await _available(c)
        [item] = available["items"]
        assert item["is_cloud"] is True and item["consent_required"] is True
        version = available["cloud_notice"]["version"]
        assert "не заменяет" in available["cloud_notice"]["text"]
        r = await c.post(f"/api/models/{cloud_model['id']}/cloud-consent", json={"notice_version": "999"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "notice_outdated"
        r = await c.post(f"/api/models/{cloud_model['id']}/cloud-consent", json={"notice_version": version})
        assert r.status_code == 201, r.text
        assert (await _available(c))["items"][0]["consent_required"] is False
    async with sa:
        # Новый текст предупреждения → новая версия → подтверждение требуется снова.
        r = await sa.put(
            "/api/admin/llm-settings",
            json={"cloud_notice_text": "Новый текст предупреждения о передаче данных."},
        )
        assert r.json()["cloud_notice"]["version"] == str(int(version) + 1)
    async with c:
        assert (await _available(c))["items"][0]["consent_required"] is True

    async with api.factory() as db:
        user = await db.get(User, analyst.id)
        assert user is not None
        with pytest.raises(AppError) as exc:
            await svc.resolve_model_for_use(db, user, uuid.UUID(cloud_model["id"]), cloud_consent=False)
        assert exc.value.code == "cloud_consent_required"
        model, provider = await svc.resolve_model_for_use(
            db, user, uuid.UUID(cloud_model["id"]), cloud_consent=True
        )
        snapshot = svc.config_snapshot(model, provider)
    dumped = json.dumps(snapshot)
    assert API_KEY not in dumped and "secret" not in dumped
    assert snapshot["provider"]["host"] == "openrouter.ai" and snapshot["provider"]["is_cloud"] is True
    assert snapshot["model"]["external_model_id"] == cloud_model["external_model_id"]

    async with sa:
        await sa.put("/api/admin/llm-settings", json={"cloud_models_enabled": False})
    async with api.factory() as db:
        user = await db.get(User, analyst.id)
        assert user is not None
        with pytest.raises(AppError) as exc:
            await svc.resolve_model_for_use(db, user, uuid.UUID(cloud_model["id"]), cloud_consent=True)
        assert exc.value.code == "cloud_models_disabled"
    actions = [r[0] for r in await api.rows("SELECT action FROM audit_log ORDER BY id")]
    assert "settings.updated" in actions and "llm.cloud_consent_recorded" in actions


async def test_capabilities_in_me(api: ApiEnv) -> None:
    for role, expected in (
        (UserRole.superadmin, (True, True, True)),
        (UserRole.admin, (True, False, False)),
        (UserRole.analyst, (False, False, False)),
    ):
        c = await api.login(await api.create_user(role))
        async with c:
            caps = (await c.get("/api/auth/me")).json()["capabilities"]
        assert (caps["manage_models"], caps["manage_providers"], caps["manage_llm_settings"]) == expected


async def test_rotate_secrets_reencrypts_with_new_primary_key(api: ApiEnv) -> None:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from app.security.secretbox import SecretBox

    c = await _superadmin(api)
    async with c:
        provider = await _openrouter(c)
    old_keys = list(api.settings.secrets_encryption_keys)
    new_key = SecretStr(Fernet.generate_key().decode())
    new_box = SecretBox([new_key, *old_keys])
    async with api.factory() as db:
        assert await svc.rotate_provider_secrets(db, new_box) == (1, 0)
        assert await svc.rotate_provider_secrets(db, new_box) == (0, 0)
    ciphertext, key_id = (
        await api.rows(
            "SELECT secret_ciphertext, secret_key_id FROM provider_configs WHERE id = :i", i=provider["id"]
        )
    )[0]
    assert key_id == new_box.primary_key_id
    # Старый ключ больше не нужен: расшифровывает только новый основной.
    assert SecretBox([new_key]).decrypt(bytes(ciphertext)) == API_KEY


async def test_model_test_truncated_output_suggests_larger_limit(api: ApiEnv) -> None:
    # Регрессия: модели с рассуждением (deepseek-r1) тратили весь лимит тестового запроса на рассуждение.
    show = {"capabilities": ["completion", "thinking"], "model_info": {"qwen2.context_length": 131072}}
    truncated = {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "length"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json=show)
        return httpx.Response(200, json=truncated)

    recorder = Recorder(handler)
    use_network(api, recorder)
    c = await _superadmin(api)
    async with c:
        provider = await _ollama(c)
        model = await _model(c, provider, external_model_id="deepseek-r1:1.5b")
        body = (await c.post(f"/api/admin/models/{model['id']}/test", json={"generate": True})).json()
        limited = await _model(c, provider, external_model_id="deepseek-r1:8b", max_output_tokens=512)
        await c.post(f"/api/admin/models/{limited['id']}/test", json={"generate": True})
    assert body["ok"] is False
    assert body["steps"][-1]["code"] == "output_truncated"
    assert "Максимум токенов ответа" in body["model"]["last_test_error_safe"]
    assert body["model"]["structured_output"] == "unknown"
    chats = [json.loads(r.content) for r in recorder.requests if r.url.path == "/api/chat"]
    # Один запрос на модель: при обрезке другие режимы не перебираются.
    assert [c["options"]["num_predict"] for c in chats] == [2048, 512]
