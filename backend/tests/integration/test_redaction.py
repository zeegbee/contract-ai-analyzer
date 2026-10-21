"""Маскировка перед облачным анализом (этап 10): настройки, предпросмотр, конвейер, права, хранение."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.llm.base import StructuredRequest
from app.models.enums import UserRole
from app.security.secretbox import SecretBox
from app.services import redaction as svc
from tests.analysis_helpers import (
    CONTRACT,
    Doc,
    FakeLLM,
    accept_notice,
    analysis_body,
    by_fragment,
    confirmed_preview,
    create_preview,
    enable_cloud,
    get_analysis,
    key,
    make_document,
    make_model,
    risk,
    risk_list,
    run_jobs,
    scan_previews,
    user_text,
)
from tests.integration.conftest import ApiClient, ApiEnv

pytestmark = pytest.mark.integration

PREAMBLE = (
    "ООО «Альфа», в лице генерального директора Иванова Ивана Петровича, действующего на основании "
    "Устава, именуемое в дальнейшем «Заказчик», с одной стороны, и ООО «Бета», именуемое в дальнейшем "
    "«Исполнитель», с другой стороны, заключили настоящий договор о нижеследующем."
)
REQUISITES = "Реквизиты Заказчика: ИНН 7707083893, тел. +7 (495) 123-45-67, e-mail: office@alfa-service.ru."
SECRET_VALUES = ("Альфа", "Бета", "Иванов", "7707083893", "123-45-67", "office@alfa-service.ru")
REDACTION_CONTRACT = [
    CONTRACT[0],
    ("paragraph", PREAMBLE, None, 1),
    *CONTRACT[2:],
    ("paragraph", REQUISITES, None, 2),
]
UNILATERAL = "Исполнитель вправе в одностороннем порядке изменить стоимость услуг"
_FRAGMENT = re.compile(r'<fragment id="(F\d{6})"[^>]*>(.*?)</fragment>', re.DOTALL)


def prompt_fragment(req: StructuredRequest, fragment_key: str) -> str | None:
    for k, body in _FRAGMENT.findall(user_text(req)):
        if k == fragment_key:
            return str(body)
    return None


def all_prompt_text(fake: FakeLLM) -> str:
    return "\n".join(m.content for c in fake.calls for m in c.messages)


async def cloud_setup(api: ApiEnv, role: UserRole = UserRole.analyst) -> tuple[Any, ApiClient, Doc, Any, Any]:
    user = await api.create_user(role)
    client = await api.login(user)
    doc = await make_document(api, user, REDACTION_CONTRACT)
    provider, model = await make_model(api, cloud=True)
    await enable_cloud(api)
    await accept_notice(api, user, provider)
    return user, client, doc, provider, model


async def start_cloud(client: ApiClient, doc: Doc, model: Any, preview_id: str | None, **extra: Any) -> Any:
    body = analysis_body(doc, model, cloud_consent=True, **extra)
    if preview_id:
        body["redaction_preview_id"] = preview_id
    return await client.post("/api/analyses", json=body)


# ================================================================== настройки


async def test_settings_roles_revisions_and_audit(api: ApiEnv) -> None:
    root = await api.create_user(UserRole.superadmin)
    admin = await api.create_user(UserRole.admin)
    admin_client = await api.login(admin)
    root_client = await api.login(root)

    r = await admin_client.get("/api/admin/redaction")
    assert r.status_code == 200
    data = r.json()
    assert data["can_manage"] is False and data["revision"] == 0
    assert data["settings"]["allow_unmasked"] is False
    assert data["settings"]["categories"]["person_name"] is True
    assert data["settings"]["categories"]["money_amount"] is False
    assert {c["code"] for c in data["categories"]} >= {"person_name", "inn_org", "dates"}
    r = await admin_client.put("/api/admin/redaction", json=data["settings"])
    assert r.status_code == 403

    settings = dict(data["settings"])
    settings["categories"] = {**settings["categories"], "money_amount": True}
    settings["custom_dictionary"] = [{"text": "Проект Полярная звезда", "category": "goods_and_projects"}]
    r = await root_client.put("/api/admin/redaction", json=settings)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1 and r.json()["can_manage"] is True
    diff = await api.scalar(
        "SELECT diff::text FROM config_revisions WHERE entity_type = 'setting' AND entity_id = 'redaction'"
    )
    assert "money_amount" in diff and "Полярная" not in diff  # значения словаря не пишутся
    audit = await api.scalar(
        "SELECT details::text FROM audit_log WHERE action = 'redaction.settings_updated'"
    )
    assert "Полярная" not in audit

    # Неизвестная категория и небезопасный шаблон отклоняются.
    r = await root_client.put("/api/admin/redaction", json={**settings, "categories": {"nope": True}})
    assert r.status_code == 422
    r = await root_client.put(
        "/api/admin/redaction",
        json={**settings, "custom_patterns": [{"pattern": r"(a)\1", "category": "email"}]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_invalid_pattern"

    # Включение отправки без маскировки — только с подтверждением.
    r = await root_client.put("/api/admin/redaction", json={**settings, "allow_unmasked": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "redaction_unmasked_confirmation_required"
    r = await root_client.put(
        "/api/admin/redaction", json={**settings, "allow_unmasked": True, "confirm_allow_unmasked": True}
    )
    assert r.status_code == 200 and r.json()["settings"]["allow_unmasked"] is True
    r = await root_client.put("/api/admin/redaction", json={**settings, "allow_unmasked": False})
    assert r.status_code == 200 and r.json()["settings"]["allow_unmasked"] is False
    actions = await api.rows("SELECT action FROM audit_log WHERE action LIKE 'redaction.%' ORDER BY id")
    assert [a[0] for a in actions] == [
        "redaction.settings_updated",
        "redaction.settings_updated",
        "redaction.unmasked_allowed",
        "redaction.settings_updated",
        "redaction.unmasked_disallowed",
    ]

    # Конфигурация для экрана предпросмотра — любому пользователю, без словарей и шаблонов.
    viewer = await api.create_user(UserRole.viewer)
    r = await (await api.login(viewer)).get("/api/redaction/config")
    assert r.status_code == 200
    assert set(r.json()) == {"allow_unmasked", "unmasked_notice_version", "preview_ttl_hours", "categories"}


# ================================================================== предпросмотр


async def test_preview_scan_access_and_encryption(api: ApiEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    owner, client, doc, _provider, _model = await cloud_setup(api)
    preview = await create_preview(client, doc)
    assert preview["status"] == "scanning" and preview["entities"] == []
    assert await scan_previews(api) == 1
    r = await client.get(f"/api/redaction-previews/{preview['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready" and data["stale"] is False and data["ner_available"] is True
    values = {o["text"]: e["category"] for e in data["entities"] for o in e["occurrences"]}
    assert values["Альфа"] == "org_name" and values["Бета"] == "org_name"
    assert values["Иванова Ивана Петровича"] == "signatory"
    assert values["7707083893"] == "inn_org" and values["+7 (495) 123-45-67"] == "phone"
    assert data["counts"]["values"] == len(data["entities"])

    # Значения хранятся только зашифрованными; счётчики, аудит и логи — без значений.
    row = (await api.rows("SELECT entities_encrypted, counts::text FROM redaction_previews"))[0]
    for value in SECRET_VALUES:
        assert value.encode() not in bytes(row[0]) and value not in row[1]
    for value in SECRET_VALUES:
        assert value not in caplog.text

    # Чужой предпросмотр — 404: даже при праве анализа документа и для Superadmin.
    colleague = await api.create_user(UserRole.analyst)
    r = await client.put(
        f"/api/documents/{doc.document_id}/access", json={"email": colleague.email, "permission": "analyze"}
    )
    assert r.status_code in (200, 201), r.text
    root = await api.create_user(UserRole.superadmin)
    viewer = await api.create_user(UserRole.viewer)
    for other in (colleague, root, viewer):
        other_client = await api.login(other)
        r = await other_client.get(f"/api/redaction-previews/{preview['id']}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "redaction_preview_not_found"
        r = await other_client.post(
            f"/api/redaction-previews/{preview['id']}/confirm", json={"reviewed": True}
        )
        assert r.status_code == 404
    # Viewer не может создать предпросмотр (нет права анализа).
    r = await (await api.login(viewer)).post(
        f"/api/documents/{doc.document_id}/versions/{doc.version_id}/redaction-previews"
    )
    assert r.status_code in (403, 404)
    assert owner is not None


async def test_preview_edits_and_confirmation(api: ApiEnv) -> None:
    _owner, client, doc, _provider, _model = await cloud_setup(api)
    preview = await create_preview(client, doc)
    await scan_previews(api)
    url = f"/api/redaction-previews/{preview['id']}"
    data = (await client.get(url)).json()
    beta = next(e for e in data["entities"] if e["canonical"] == "Бета")

    # Снять пометку с сущности; добавить ручную пометку со всеми вхождениями.
    preamble_key = key(1)
    start = PREAMBLE.index("Устава")
    r = await client.patch(
        url,
        json={
            "ops": [
                {"op": "set_entity", "entity_id": beta["id"], "masked": False},
                {
                    "op": "add",
                    "fragment": preamble_key,
                    "start": start,
                    "end": start + len("Устава"),
                    "category": "goods_and_projects",
                    "all_occurrences": True,
                },
            ]
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    manual = next(e for e in data["entities"] if e["source"] == "manual")
    assert manual["canonical"] == "Устава" and manual["category"] == "goods_and_projects"
    assert data["counts"]["manual"] == 1 and data["counts"]["unmarked"] == 1

    # Границы проверяет сервер; частичное пересечение с найденным значением — 409.
    alfa_start = PREAMBLE.index("Альфа")
    for op, status, code in (
        ({"fragment": preamble_key, "start": 0, "end": 10_000}, 422, "redaction_invalid_mark"),
        ({"fragment": "F999999", "start": 0, "end": 3}, 422, "redaction_invalid_mark"),
        (
            {"fragment": preamble_key, "start": alfa_start + 2, "end": alfa_start + 12},
            409,
            "redaction_mark_overlaps",
        ),
    ):
        r = await client.patch(url, json={"ops": [{"op": "add", "category": "org_name", **op}]})
        assert r.status_code == status and r.json()["error"]["code"] == code, (op, r.text)
    r = await client.patch(url, json={"ops": [{"op": "set_entity", "entity_id": "E9999", "masked": False}]})
    assert r.status_code == 422

    # Без отметки «проверено» — 422; без разрешения — отправка без маскировки запрещена.
    r = await client.post(f"{url}/confirm", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_review_required"
    r = await client.post(f"{url}/confirm", json={"reviewed": True, "mode": "unmasked"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "redaction_unmasked_not_allowed"
    r = await client.post(f"{url}/confirm", json={"reviewed": True})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["mode"] == "masked"
    # После подтверждения правки недоступны.
    r = await client.patch(url, json={"ops": [{"op": "set_entity", "entity_id": beta["id"], "masked": True}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "redaction_preview_not_editable"
    audit = await api.scalar(
        "SELECT details::text FROM audit_log WHERE action = 'redaction.preview_confirmed'"
    )
    assert '"mode": "masked"' in audit and '"manual_added": 1' in audit
    for value in SECRET_VALUES:
        assert value not in audit


# ================================================================== запуск анализа


async def test_cloud_analysis_preview_checks(api: ApiEnv) -> None:
    owner, client, doc, provider, model = await cloud_setup(api)
    r = await start_cloud(client, doc, model, None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_preview_required"
    # Не подтверждён.
    preview = await create_preview(client, doc)
    await scan_previews(api)
    r = await start_cloud(client, doc, model, preview["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_preview_required"
    # Чужой предпросмотр — 404.
    other = await api.create_user(UserRole.analyst)
    other_client = await api.login(other)
    other_doc = await make_document(api, other, REDACTION_CONTRACT)
    await accept_notice(api, other, provider)
    foreign = await confirmed_preview(api, other_client, other_doc)
    r = await start_cloud(client, doc, model, foreign)
    assert r.status_code == 404 and r.json()["error"]["code"] == "redaction_preview_not_found"
    # Предпросмотр другой версии (своего другого документа) — 422.
    second = await make_document(api, owner, REDACTION_CONTRACT)
    own_other = await confirmed_preview(api, client, second)
    r = await start_cloud(client, doc, model, own_other)
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_preview_mismatch"
    # Настройки изменились после подтверждения — 409.
    preview_id = await confirmed_preview(api, client, doc)
    root = await api.create_user(UserRole.superadmin)
    root_client = await api.login(root)
    settings = (await root_client.get("/api/admin/redaction")).json()["settings"]
    settings["categories"]["dates"] = True
    assert (await root_client.put("/api/admin/redaction", json=settings)).status_code == 200
    r = await start_cloud(client, doc, model, preview_id)
    assert r.status_code == 409 and r.json()["error"]["code"] == "redaction_settings_changed"
    r = await client.get(f"/api/redaction-previews/{preview_id}")
    assert r.json()["stale"] is True and r.json()["stale_reason"] == "settings_changed"
    # Истёк срок — 409.
    preview_id = await confirmed_preview(api, client, doc)
    await api.execute(
        "UPDATE redaction_previews SET expires_at = now() - interval '1 minute' WHERE id = :i", i=preview_id
    )
    r = await start_cloud(client, doc, model, preview_id)
    assert r.status_code == 409 and r.json()["error"]["code"] == "redaction_preview_expired"
    # Предпросмотр расходуется: второй анализ с тем же предпросмотром — 409.
    preview_id = await confirmed_preview(api, client, doc)
    r = await start_cloud(client, doc, model, preview_id)
    assert r.status_code == 201, r.text
    r = await start_cloud(client, doc, model, preview_id)
    assert r.status_code == 409 and r.json()["error"]["code"] == "redaction_preview_used"
    # Найденные данные предпросмотра после запуска удалены, таблица соответствий — у анализа.
    assert (
        await api.scalar("SELECT entities_encrypted FROM redaction_previews WHERE id = :i", i=preview_id)
        is None
    )
    assert (
        await api.scalar("SELECT count(*) FROM analysis_redactions WHERE preview_id = :i", i=preview_id) == 1
    )
    # Локальная модель — предпросмотр не нужен и не принимается.
    _local_provider, local = await make_model(api)
    r = await client.post(
        "/api/analyses", json={**analysis_body(doc, local), "redaction_preview_id": own_other}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_not_applicable"


def masked_chunk_handler(req: StructuredRequest) -> dict[str, Any]:
    """Риск по преамбуле: цитата и формулировки — как их видит модель (с метками)."""
    body = prompt_fragment(req, key(1))
    if body is None:
        return by_fragment({key(6): [risk("Одностороннее изменение цены", [(key(6), UNILATERAL)])]})(req)
    quote = body[: body.index("действующего") + len("действующего")]
    label = re.search(r"⟦ПОДПИСАНТ_\d+⟧", quote)
    assert label is not None
    return {
        "risks": [
            risk(
                f"Полномочия {label.group(0)} не подтверждены",
                [(key(1), quote)],
                category="other",
                description=f"Договор подписывает {label.group(0)}; документы о полномочиях не приложены.",
            )
        ],
        "notes": None,
    }


async def test_masked_pipeline_sends_no_values_and_restores_quotes(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    preview_id = await confirmed_preview(api, client, doc)
    created = (await start_cloud(client, doc, model, preview_id)).json()
    assert created["redaction"]["mode"] == "masked" and created["redaction"]["values"] >= 5
    assert created["config_snapshot"]["redaction"]["mode"] == "masked"
    fake = FakeLLM({"chunk_risks": masked_chunk_handler})
    await run_jobs(api, fake)

    # Ни одного замаскированного значения в запросах к модели; метки — есть.
    sent = all_prompt_text(fake)
    for value in SECRET_VALUES:
        assert value not in sent, value
    assert "⟦ОРГАНИЗАЦИЯ_1⟧" in sent and "⟦ПОДПИСАНТ_1⟧" in sent
    assert "сохраняй метки дословно" in sent
    verifier = fake.by_schema("evidence_relevance")
    assert verifier and all("⟦" in user_text(c) for c in verifier[:1])

    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] in ("completed", "completed_with_warnings"), analysis
    items = (await risk_list(client, created["id"]))["items"]
    authority = next(r for r in items if r["title"].startswith("Полномочия"))
    # Формулировки — с исходными значениями (каноническая форма), цитата — дословный исходный текст.
    assert authority["title"] == "Полномочия Иванов Иван Петрович не подтверждены"
    assert "Иванов Иван Петрович" in authority["description"]
    evidence = authority["evidence"][0]
    expected = PREAMBLE[: PREAMBLE.index("действующего") + len("действующего")]
    assert evidence["quote"] == expected
    assert evidence["match_status"] == "exact" and evidence["fragment_key"] == key(1)
    assert evidence["page_start"] == 1 and "redaction_masked_quote" in evidence["match_notes"]
    assert authority["validation_status"] == "verified_match"
    # Сводка — с исходными наименованиями сторон.
    parties = analysis["summary"]["structure"]["parties"] if "structure" in analysis["summary"] else []
    assert all("⟦" not in str(p) for p in parties)
    assert "⟦" not in str(analysis["summary"]) and "⟦" not in str(analysis["coverage"])
    # В БД в открытом виде нет таблицы соответствий.
    mapping = await api.scalar("SELECT mapping_encrypted FROM analysis_redactions")
    for value in SECRET_VALUES:
        assert value.encode() not in bytes(mapping)


async def test_distorted_label_triggers_quote_retry_and_retry_reuses_mapping(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    preview_id = await confirmed_preview(api, client, doc)
    created = (await start_cloud(client, doc, model, preview_id)).json()
    mapping_before = await api.scalar("SELECT mapping_encrypted FROM analysis_redactions")

    def chunk(req: StructuredRequest) -> dict[str, Any]:
        if prompt_fragment(req, key(1)) is None:
            return {"risks": [], "notes": None}
        # Модель «склонила» метку — цитата не совпадает с текстом.
        bad = "ООО «⟦ОРГАНИЗАЦИЯ_9⟧», в лице генерального директора"
        return {"risks": [risk("Риск полномочий", [(key(1), bad)], category="other")], "notes": None}

    def retry(req: StructuredRequest) -> dict[str, Any]:
        body = prompt_fragment(req, key(1)) or ""
        quote = body[: body.index(", действующего")]
        return {"withdraw": False, "evidence": [{"fragment_id": key(1), "quote": quote}], "comment": None}

    fake = FakeLLM({"chunk_risks": chunk, "quote_retry": retry})
    await run_jobs(api, fake)
    assert fake.by_schema("quote_retry"), "искажённая метка должна вызвать повтор цитаты"
    items = (await risk_list(client, created["id"], include_rejected=True))["items"]
    current = [e for e in items[0]["evidence"] if not e["superseded"]]
    assert current[0]["match_status"] == "exact"
    assert current[0]["quote"].startswith(
        "ООО «Альфа», в лице генерального директора Иванова Ивана Петровича"
    )
    superseded = [e for e in items[0]["evidence"] if e["superseded"]]
    assert superseded[0]["match_status"] == "not_found"
    assert "redaction_unknown_label" in superseded[0]["match_notes"]

    # Повтор анализа использует ту же таблицу соответствий (без нового поиска).
    await api.execute("UPDATE analyses SET status = 'failed'")
    r = await client.post(f"/api/analyses/{created['id']}/retry", json={"cloud_consent": True})
    assert r.status_code == 200, r.text
    assert (
        await api.scalar("SELECT count(*) FROM jobs WHERE type = 'redaction_scan' AND status = 'queued'") == 0
    )
    assert await api.scalar("SELECT mapping_encrypted FROM analysis_redactions") == mapping_before


async def test_unmasked_send_requires_setting_and_confirmation(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    root = await api.create_user(UserRole.superadmin)
    root_client = await api.login(root)
    settings = (await root_client.get("/api/admin/redaction")).json()["settings"]
    r = await root_client.put(
        "/api/admin/redaction", json={**settings, "allow_unmasked": True, "confirm_allow_unmasked": True}
    )
    assert r.status_code == 200
    config = (await client.get("/api/redaction/config")).json()
    assert config["allow_unmasked"] is True

    preview = await create_preview(client, doc)
    await scan_previews(api)
    url = f"/api/redaction-previews/{preview['id']}/confirm"
    r = await client.post(url, json={"reviewed": True, "mode": "unmasked"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "redaction_unmasked_confirmation_required"
    r = await client.post(
        url,
        json={
            "reviewed": True,
            "mode": "unmasked",
            "confirm_unmasked": True,
            "unmasked_notice_version": "old",
        },
    )
    assert r.status_code == 422
    r = await client.post(
        url,
        json={
            "reviewed": True,
            "mode": "unmasked",
            "confirm_unmasked": True,
            "unmasked_notice_version": config["unmasked_notice_version"],
        },
    )
    assert r.status_code == 200 and r.json()["mode"] == "unmasked_by_user"
    created = (await start_cloud(client, doc, model, preview["id"])).json()
    assert (
        created["redaction"]["mode"] == "unmasked_by_user" and created["redaction"]["unmasked_confirmed_at"]
    )
    row = (await api.rows("SELECT mode, unmasked_confirmed_at, mapping_encrypted FROM analysis_redactions"))[
        0
    ]
    assert row[0] == "unmasked_by_user" and row[1] is not None and row[2] is None
    audit = await api.scalar("SELECT details::text FROM audit_log WHERE action = 'redaction.sent_unmasked'")
    assert audit and "Альфа" not in audit
    fake = FakeLLM()
    await run_jobs(api, fake)
    assert "Альфа" in all_prompt_text(fake)  # отправлено без маскировки по подтверждению пользователя
    assert "сохраняй метки" not in all_prompt_text(fake)


async def test_local_model_sends_original_text_without_preview(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    doc = await make_document(api, user, REDACTION_CONTRACT)
    _provider, model = await make_model(api)
    r = await client.post("/api/analyses", json=analysis_body(doc, model))
    assert r.status_code == 201 and r.json()["redaction"] is None
    assert r.json()["config_snapshot"]["redaction"] is None
    fake = FakeLLM()
    await run_jobs(api, fake)
    assert "Иванова Ивана Петровича" in all_prompt_text(fake)
    assert await api.scalar("SELECT count(*) FROM redaction_previews") == 0


# ================================================================== хранение и обслуживание


async def test_document_deletion_removes_previews_and_mappings(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    preview_id = await confirmed_preview(api, client, doc)
    assert (await start_cloud(client, doc, model, preview_id)).status_code == 201
    await create_preview(client, doc)
    assert await api.scalar("SELECT count(*) FROM redaction_previews") == 2
    r = await client.delete(f"/api/documents/{doc.document_id}")
    assert r.status_code == 202, r.text
    await run_jobs(api, FakeLLM(), limit=3, job_types=("purge_document",))
    assert await api.scalar("SELECT count(*) FROM redaction_previews") == 0
    assert await api.scalar("SELECT count(*) FROM analysis_redactions") == 0


async def test_expired_previews_are_deleted_by_housekeeping(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    used = await confirmed_preview(api, client, doc)
    assert (await start_cloud(client, doc, model, used)).status_code == 201
    unused = (await create_preview(client, doc))["id"]
    await api.execute("UPDATE redaction_previews SET expires_at = now() - interval '1 hour'")
    assert await svc.delete_expired_previews(api.factory) == 1
    remaining = {str(r[0]) for r in await api.rows("SELECT id FROM redaction_previews")}
    assert remaining == {used} and unused not in remaining


async def test_rotate_secrets_reencrypts_redaction_data(api: ApiEnv) -> None:
    _owner, client, doc, _provider, model = await cloud_setup(api)
    preview_id = await confirmed_preview(api, client, doc)
    assert (await start_cloud(client, doc, model, preview_id)).status_code == 201
    await create_preview(client, doc)
    await scan_previews(api)
    old_keys = list(api.settings.secrets_encryption_keys)
    new_key = Fernet.generate_key().decode()
    box = SecretBox([SecretStr(new_key), *old_keys])
    async with api.factory() as db:
        rotated, failed = await svc.rotate_secrets(db, box)
    assert (rotated, failed) == (2, 0)
    only_new = SecretBox([SecretStr(new_key)])
    mapping = await api.scalar("SELECT mapping_encrypted FROM analysis_redactions")
    assert "labels" in only_new.decrypt(bytes(mapping))
    ids = {
        r[0]
        for r in await api.rows(
            "SELECT secret_key_id FROM redaction_previews WHERE entities_encrypted IS NOT NULL"
        )
    }
    assert ids == {box.primary_key_id}
    assert uuid.UUID(preview_id)
