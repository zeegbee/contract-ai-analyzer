"""API анализов: права и IDOR, выбор модели и облако, лимиты, идемпотентность, оценка, список."""

from __future__ import annotations

import uuid
from typing import Any

from app.models.enums import ExtractionStatus, UserRole
from tests.analysis_helpers import (
    FakeLLM,
    accept_notice,
    analysis_body,
    enable_cloud,
    make_document,
    make_model,
    run_jobs,
    start,
)
from tests.integration.conftest import ApiEnv

PRICING = {
    "prompt_per_mtok": "0.15",
    "completion_per_mtok": "0.6",
    "currency": "USD",
    "source": "provider_api",
    "fetched_at": "2026-10-16T00:00:00+00:00",
}


async def test_roles_and_document_access(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    owner_client = await api.login(owner)
    doc = await make_document(api, owner)
    _provider, model = await make_model(api)
    body = analysis_body(doc, model)

    viewer = await api.create_user(UserRole.viewer)
    viewer_client = await api.login(viewer)
    r = await viewer_client.post("/api/analyses", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"

    stranger = await api.create_user(UserRole.analyst)
    stranger_client = await api.login(stranger)
    r = await stranger_client.post("/api/analyses", json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "document_not_found"

    admin = await api.create_user(UserRole.admin)
    admin_client = await api.login(admin)
    r = await admin_client.post("/api/analyses", json=body)
    assert r.status_code == 404  # Admin не анализирует чужие договоры без явного доступа

    created = await start(owner_client, doc, model)
    analysis_id = created["id"]
    for client in (stranger_client, admin_client, viewer_client):
        assert (await client.get(f"/api/analyses/{analysis_id}")).status_code == 404
        assert (await client.get(f"/api/analyses/{analysis_id}/risks")).status_code == 404
    assert (await stranger_client.post(f"/api/analyses/{analysis_id}/cancel")).status_code == 404
    assert (await stranger_client.post(f"/api/analyses/{analysis_id}/retry", json={})).status_code == 404
    assert (await owner_client.get(f"/api/analyses/{uuid.uuid4()}")).status_code == 404

    # Доступ «view»: отчёт виден, запуск и отмена — нет.
    r = await owner_client.put(
        f"/api/documents/{doc.document_id}/access", json={"email": viewer.email, "permission": "view"}
    )
    assert r.status_code in (200, 201), r.text
    r = await viewer_client.get(f"/api/analyses/{analysis_id}")
    assert r.status_code == 200 and r.json()["can_cancel"] is False
    assert (await viewer_client.post(f"/api/analyses/{analysis_id}/cancel")).status_code == 403
    r = await owner_client.put(
        f"/api/documents/{doc.document_id}/access", json={"email": stranger.email, "permission": "view"}
    )
    assert r.status_code in (200, 201)
    assert (await stranger_client.post("/api/analyses", json=body)).status_code == 404
    assert (await stranger_client.post(f"/api/analyses/{analysis_id}/cancel")).status_code == 404

    # Доступ «analyze» позволяет запуск.
    r = await owner_client.put(
        f"/api/documents/{doc.document_id}/access", json={"email": stranger.email, "permission": "analyze"}
    )
    assert r.status_code in (200, 201)
    assert (await stranger_client.post("/api/analyses", json=body)).status_code == 201


async def test_superadmin_access_to_foreign_analysis_is_audited(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _provider, model = await make_model(api)
    created = await start(client, doc, model)
    root = await api.create_user(UserRole.superadmin)
    root_client = await api.login(root)
    assert (await root_client.get(f"/api/analyses/{created['id']}")).status_code == 200
    assert (await root_client.get(f"/api/analyses/{created['id']}/risks")).status_code == 200
    assert (await root_client.post("/api/analyses", json=analysis_body(doc, model))).status_code == 201
    vias = await api.rows(
        "SELECT details->>'via' FROM audit_log WHERE action = 'document.accessed_by_superadmin' ORDER BY id"
    )
    assert [v[0] for v in vias] == ["analysis", "analysis_risks", "analysis_start"]
    # В общем списке Superadmin чужие анализы не видит (только по ID).
    assert (await root_client.get("/api/analyses")).json()["items"] == []


async def test_version_must_be_ready_and_belong_to_document(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    _provider, model = await make_model(api)
    pending = await make_document(api, owner, extraction_status=ExtractionStatus.processing)
    r = await client.post("/api/analyses", json=analysis_body(pending, model))
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_not_ready"
    doc = await make_document(api, owner)
    other = await make_document(api, owner)
    r = await client.post("/api/analyses", json=analysis_body(doc, model, version_id=str(other.version_id)))
    assert r.status_code == 404 and r.json()["error"]["code"] == "version_not_found"


async def test_model_selection_rules(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, admins_only = await make_model(api, roles=["admin", "superadmin"])
    r = await client.post("/api/analyses", json=analysis_body(doc, admins_only))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    _p, disabled = await make_model(api, enabled=False)
    r = await client.post("/api/analyses", json=analysis_body(doc, disabled))
    assert r.status_code == 409 and r.json()["error"]["code"] == "model_disabled"
    _p, verifier_only = await make_model(api, purpose=["verifier"])
    r = await client.post("/api/analyses", json=analysis_body(doc, verifier_only))
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_purpose_mismatch"
    _p, model = await make_model(api)
    r = await client.post(
        "/api/analyses", json=analysis_body(doc, model, verifier_model_id=str(verifier_only.id))
    )
    assert r.status_code == 201
    snapshot = r.json()["config_snapshot"]
    assert snapshot["verifier"]["same_as_analysis"] is False
    assert snapshot["verifier"]["model"]["id"] == str(verifier_only.id)
    r = await client.post(
        "/api/analyses", json=analysis_body(doc, model, verifier_model_id=str(admins_only.id))
    )
    assert r.status_code == 404


async def test_cloud_model_requires_switch_notice_and_consent(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    provider, model = await make_model(api, cloud=True)
    body = analysis_body(doc, model)
    r = await client.post("/api/analyses", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_models_disabled"
    await enable_cloud(api)
    r = await client.post("/api/analyses", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_consent_required"
    r = await client.post("/api/analyses", json={**body, "cloud_consent": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_notice_not_accepted"
    await accept_notice(api, owner, provider)
    r = await client.post("/api/analyses", json={**body, "cloud_consent": True})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["cloud_transfer"] is True and created["cloud_consent_at"]
    assert created["config_snapshot"]["provider"]["is_cloud"] is True
    # Повтор облачного анализа требует нового подтверждения.
    await api.execute("UPDATE analyses SET status = 'failed'")
    await api.execute("UPDATE jobs SET status = 'failed'")
    r = await client.post(f"/api/analyses/{created['id']}/retry", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_consent_required"
    r = await client.post(f"/api/analyses/{created['id']}/retry", json={"cloud_consent": True})
    assert r.status_code == 200 and r.json()["status"] == "queued"


async def test_local_model_needs_no_consent(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api)
    created = await start(client, doc, model)
    assert created["cloud_transfer"] is False and created["cloud_consent_at"] is None


async def test_request_validation(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api)
    cases: list[dict[str, Any]] = [
        analysis_body(doc, model, party="named"),
        analysis_body(doc, model, party="party_a", party_name="ООО «Бета»"),
        analysis_body(doc, model, categories=[]),
        analysis_body(doc, model, categories=["payment", "payment"]),
        analysis_body(doc, model, categories=["unknown_category"]),
        analysis_body(doc, model, depth="ultra"),
        {**analysis_body(doc, model), "temperature": 2},
    ]
    for body in cases:
        r = await client.post("/api/analyses", json=body)
        assert r.status_code == 422, (body, r.text)
    r = await client.post(
        "/api/analyses", json=analysis_body(doc, model), headers={"Idempotency-Key": "bad key!"}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_idempotency_key"
    # Без CSRF-токена запуск отклоняется.
    no_csrf = api.client()
    no_csrf.cookies = client.cookies
    r = await no_csrf.post("/api/analyses", json=analysis_body(doc, model))
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


async def test_idempotency_key(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api)
    headers = {"Idempotency-Key": "analysis-key-0001"}
    first = await client.post("/api/analyses", json=analysis_body(doc, model), headers=headers)
    assert first.status_code == 201
    again = await client.post("/api/analyses", json=analysis_body(doc, model), headers=headers)
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    other = await client.post("/api/analyses", json=analysis_body(doc, model, depth="deep"), headers=headers)
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_key_reused"
    assert await api.scalar("SELECT count(*) FROM analyses") == 1
    assert await api.scalar("SELECT count(*) FROM jobs WHERE type = 'run_analysis'") == 1
    # Ключ другого пользователя не пересекается.
    colleague = await api.create_user(UserRole.analyst)
    colleague_client = await api.login(colleague)
    colleague_doc = await make_document(api, colleague)
    r = await colleague_client.post(
        "/api/analyses", json=analysis_body(colleague_doc, model), headers=headers
    )
    assert r.status_code == 201


async def test_active_analyses_limit(api: ApiEnv) -> None:
    api.settings.analysis_max_active_per_user = 1
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api)
    await start(client, doc, model)
    r = await client.post("/api/analyses", json=analysis_body(doc, model))
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_active_analyses"
    await run_jobs(api, FakeLLM())
    assert (await client.post("/api/analyses", json=analysis_body(doc, model))).status_code == 201


async def test_token_budget_limit_and_estimate(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, priced = await make_model(api, pricing=PRICING)
    _p, unpriced = await make_model(api)
    r = await client.post("/api/analyses/estimate", json=analysis_body(doc, priced))
    assert r.status_code == 200, r.text
    estimate = r.json()
    assert estimate["chunks"] == 1 and estimate["within_limit"] is True
    assert estimate["estimated_cost"] is not None and estimate["cost_currency"] == "USD"
    assert estimate["pricing_source"] == "provider_api"
    expected = (
        0.15 * estimate["estimated_input_tokens"] + 0.6 * estimate["estimated_output_tokens"]
    ) / 1_000_000
    assert abs(float(estimate["estimated_cost"]) - expected) < 0.0001
    r = await client.post("/api/analyses/estimate", json=analysis_body(doc, unpriced))
    assert r.json()["estimated_cost"] is None and r.json()["pricing_source"] is None
    assert await api.scalar("SELECT count(*) FROM analyses") == 0  # оценка ничего не создаёт

    api.settings.analysis_max_tokens = 10_000
    r = await client.post("/api/analyses", json=analysis_body(doc, priced))
    assert r.status_code == 422 and r.json()["error"]["code"] == "analysis_budget_exceeded"
    assert r.json()["error"]["details"]["limit"] == 10_000


async def test_model_context_too_small(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api, context_window=4096, max_output_tokens=4096)
    r = await client.post("/api/analyses", json=analysis_body(doc, model))
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_context_too_small"


async def test_list_analyses(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    other_doc = await make_document(api, owner)
    _p, model = await make_model(api)
    first = await start(client, doc, model)
    second = await start(client, other_doc, model)
    items = (await client.get("/api/analyses")).json()["items"]
    assert [i["id"] for i in items] == [second["id"], first["id"]]
    assert items[0]["model_display_name"] == model.display_name and items[0]["can_cancel"] is True
    by_doc = (await client.get("/api/analyses", params={"document_id": str(doc.document_id)})).json()
    assert [i["id"] for i in by_doc["items"]] == [first["id"]]
    page = (await client.get("/api/analyses", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = (await client.get("/api/analyses", params={"limit": 1, "cursor": page["next_cursor"]})).json()
    assert [i["id"] for i in rest["items"]] == [first["id"]]
    stranger = await api.create_user(UserRole.analyst)
    stranger_client = await api.login(stranger)
    assert (await stranger_client.get("/api/analyses")).json()["items"] == []
    r = await stranger_client.get("/api/analyses", params={"document_id": str(doc.document_id)})
    assert r.status_code == 404


async def test_deleting_document_hides_analysis(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    client = await api.login(owner)
    doc = await make_document(api, owner)
    _p, model = await make_model(api)
    created = await start(client, doc, model)
    r = await client.delete(f"/api/documents/{doc.document_id}")
    assert r.status_code == 202
    assert (await client.get(f"/api/analyses/{created['id']}")).status_code == 404
    # Задача анализа удалённого документа ничего не делает.
    fake = FakeLLM()
    await run_jobs(api, fake)
    assert fake.calls == []
