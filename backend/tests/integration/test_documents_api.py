"""Загрузка, извлечение, текст, оригинал, удаление и изоляция доступа к документам (реальный PostgreSQL).

Извлечение выполняется настоящим конвейером (дочерний процесс разбора). Converter заменён
httpx.MockTransport: живая конвертация LibreOffice проверяется в Docker (см. IMPLEMENTATION_STATUS).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from app.ingestion.converter_client import ConverterClient
from app.ingestion.pipeline import ExtractionPipeline, RetryLater
from app.jobs.handlers.documents import purge_document_files_and_rows
from app.jobs.registry import get_handler
from app.models.enums import UserRole
from app.models.users import User
from app.storage import LocalStorage
from tests import docfactory as f
from tests.integration.conftest import ApiClient, ApiEnv

pytestmark = pytest.mark.integration

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME = {"pdf": "application/pdf", "docx": DOCX_MIME, "doc": "application/msword"}


async def upload(
    client: ApiClient, data: bytes, name: str, mime: str | None = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    ext = name.rsplit(".", 1)[-1]
    h = {"X-File-Name": quote(name), "Content-Type": mime if mime is not None else MIME.get(ext, "")}
    h.update(headers or {})
    return await client.post("/api/documents", content=data, headers=h)


def _unavailable(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"error": "no_soffice"})


class FakeConverter:
    """Конвертер для тестов: DOC → заранее известный DOCX, DOCX → PDF из двух страниц."""

    def __init__(self, tmp: Path, pages: list[list[str]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.docx = f.contract_docx(tmp / "converted.docx").read_bytes()
        self.pdf = f.text_pdf(tmp / "render.pdf", pages or [["Первая страница"]]).read_bytes()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        pair = (request.url.params["from"], request.url.params["to"])
        self.calls.append(pair)
        return httpx.Response(200, content=self.docx if pair == ("doc", "docx") else self.pdf)


def pipeline(api: ApiEnv, handler: Any = _unavailable) -> ExtractionPipeline:
    converter = ConverterClient("http://converter.test", 10, transport=httpx.MockTransport(handler))
    return ExtractionPipeline(api.settings, api.factory, api.app.state.storage, converter=converter)


def storage(api: ApiEnv) -> LocalStorage:
    s: LocalStorage = api.app.state.storage
    return s


def stored_files(api: ApiEnv) -> list[str]:
    root = storage(api).root
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


async def uploaded(api: ApiEnv, client: ApiClient, data: bytes, name: str) -> tuple[str, str]:
    r = await upload(client, data, name)
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"], body["current_version_id"]


async def extract(api: ApiEnv, version_id: str, handler: Any = _unavailable, *, last: bool = True) -> str:
    return await pipeline(api, handler).run(uuid.UUID(version_id), last_attempt=last)


async def fragments(client: ApiClient, doc_id: str, version_id: str, **params: Any) -> dict[str, Any]:
    r = await client.get(f"/api/documents/{doc_id}/versions/{version_id}/fragments", params=params)
    assert r.status_code == 200, r.text
    result: dict[str, Any] = r.json()
    return result


@pytest.fixture
async def analyst(api: ApiEnv) -> tuple[User, ApiClient]:
    user = await api.create_user(UserRole.analyst)
    return user, await api.login(user)


# ---------------------------------------------------------------- загрузка


async def test_upload_docx_creates_document_version_job_and_audit(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    data = f.contract_docx(tmp_path / "c.docx").read_bytes()
    r = await upload(client, data, "Договор поставки.docx")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Договор поставки"
    assert body["is_owner"] and body["can_manage"] and body["access"] == "owner"
    version = body["current_version_detail"]
    assert version["detected_format"] == "docx" and version["mime_type"] == DOCX_MIME
    assert version["extraction_status"] == "pending" and version["size_bytes"] == len(data)
    assert version["original_filename"] == "Договор поставки.docx"
    # Оригинал хранится под случайным именем, без имени файла и расширения.
    files = stored_files(api)
    assert len(files) == 1 and files[0].startswith("originals/") and "Договор" not in files[0]
    assert (storage(api).root / files[0]).read_bytes() == data
    jobs = await api.rows("SELECT type, payload->>'version_id' FROM jobs")
    assert [tuple(j) for j in jobs] == [("extract_document", version["id"])]
    audit = await api.rows("SELECT action, details FROM audit_log WHERE action = 'document.uploaded'")
    assert len(audit) == 1
    details = audit[0][1]
    assert details["sha256"] == version["sha256"] and "Договор" not in str(details)
    assert get_handler("extract_document") is not None and get_handler("purge_document") is not None


@pytest.mark.parametrize(
    ("name", "content_factory", "mime", "status", "code"),
    [
        ("empty.pdf", lambda t: b"", "application/pdf", 422, "empty_file"),
        ("page.pdf", lambda t: b"<html><script>alert(1)</script></html>", "application/pdf", 415,
         "unsupported_file_type"),
        ("real-pdf.docx", lambda t: f.text_pdf(t / "x.pdf", [["Текст"]]).read_bytes(), DOCX_MIME, 415,
         "file_type_mismatch"),
        ("spoofed.docx", lambda t: f.contract_docx(t / "x.docx").read_bytes(), "application/pdf", 415,
         "mime_type_mismatch"),
        ("script.exe", lambda t: b"MZ\x90", "application/octet-stream", 415, "unsupported_file_type"),
        ("bomb.docx", lambda t: f.zip_bomb_docx(t / "b.docx").read_bytes(), DOCX_MIME, 422,
         "archive_limits_exceeded"),
        ("macro.docx", lambda t: f.docm(t / "m.docx").read_bytes(), DOCX_MIME, 415, "macros_not_allowed"),
        ("trav.docx", lambda t: f.traversal_docx(t / "t.docx").read_bytes(), DOCX_MIME, 422,
         "unsafe_archive_path"),
        ("broken.docx", lambda t: f.contract_docx(t / "c.docx").read_bytes()[:3000], DOCX_MIME, 422,
         "corrupted_file"),
    ],
)  # fmt: skip
async def test_upload_rejections_leave_no_trace(
    api: ApiEnv,
    analyst: tuple[User, ApiClient],
    tmp_path: Path,
    name: str,
    content_factory: Any,
    mime: str,
    status: int,
    code: str,
) -> None:
    _, client = analyst
    r = await upload(client, content_factory(tmp_path), name, mime)
    assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert await api.scalar("SELECT count(*) FROM documents") == 0
    assert await api.scalar("SELECT count(*) FROM jobs") == 0
    assert stored_files(api) == []  # ни оригинала, ни временного .part


async def test_upload_size_limit(api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path) -> None:
    _, client = analyst
    api.settings.max_upload_mb = 1
    big = f.many_pages_pdf(tmp_path / "big.pdf", 1).read_bytes() + b"%" + b"0" * (2 * 1024 * 1024)
    r = await upload(client, big, "big.pdf")
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"

    # Без Content-Length (chunked): лимит проверяется потоково.
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"%PDF-1.7\n" + b"0" * (600 * 1024)

    r = await client.post(
        "/api/documents",
        content=chunks(),
        headers={"X-File-Name": "big.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 413
    assert stored_files(api) == [] and await api.scalar("SELECT count(*) FROM documents") == 0


async def test_upload_requires_contributor_role_csrf_and_filename(api: ApiEnv, tmp_path: Path) -> None:
    data = f.text_pdf(tmp_path / "t.pdf", [["Текст договора"]]).read_bytes()
    viewer = await api.login(await api.create_user(UserRole.viewer))
    r = await upload(viewer, data, "t.pdf")
    assert (r.status_code, r.json()["error"]["code"]) == (403, "permission_denied")
    analyst = await api.login(await api.create_user(UserRole.analyst))
    r = await upload(analyst, data, "t.pdf", headers={"X-CSRF-Token": "wrong"})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "csrf_failed")
    r = await analyst.post("/api/documents", content=data, headers={"Content-Type": "application/pdf"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "file_name_required")
    anonymous = api.client()
    r = await anonymous.post("/api/documents", content=data, headers={"X-File-Name": "t.pdf"})
    assert r.status_code == 401
    assert stored_files(api) == []


async def test_upload_idempotency_key(api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path) -> None:
    _, client = analyst
    data = f.text_pdf(tmp_path / "t.pdf", [["Текст договора"]]).read_bytes()
    key = {"Idempotency-Key": "upload-" + uuid.uuid4().hex}
    first = await upload(client, data, "t.pdf", headers=key)
    second = await upload(client, data, "t.pdf", headers=key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert await api.scalar("SELECT count(*) FROM documents") == 1
    assert len(stored_files(api)) == 1
    r = await upload(client, data, "t.pdf", headers={"Idempotency-Key": "bad key!"})
    assert r.status_code == 400


async def test_pending_extraction_limit(api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path) -> None:
    _, client = analyst
    api.settings.max_pending_extractions_per_user = 2
    data = f.text_pdf(tmp_path / "t.pdf", [["Текст договора"]]).read_bytes()
    assert (await upload(client, data, "a.pdf")).status_code == 201
    assert (await upload(client, data, "b.pdf")).status_code == 201
    r = await upload(client, data, "c.pdf")
    assert (r.status_code, r.json()["error"]["code"]) == (429, "too_many_pending_extractions")


# ---------------------------------------------------------------- извлечение


async def test_docx_extraction_end_to_end(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    doc_id, version_id = await uploaded(
        api, client, f.contract_docx(tmp_path / "c.docx").read_bytes(), "c.docx"
    )
    # Разметки Word нет → рендер через converter (здесь недоступен) → страницы не указываются.
    assert await extract(api, version_id) == "ready"
    detail = (await client.get(f"/api/documents/{doc_id}")).json()["current_version_detail"]
    assert detail["extraction_status"] == "ready" and detail["extractor_version"]
    assert detail["page_source"] is None and detail["page_count"] is None
    assert {"code": "pages_unavailable", "reason": "converter_unavailable"} in detail["warnings"]
    items = (await fragments(client, doc_id, version_id))["items"]
    texts = [i["text"] for i in items]
    assert (
        texts.index(f.CONTRACT_PARAGRAPHS[1])
        < texts.index("№ | Товар | Цена, руб.")
        < texts.index("Текст после таблицы.")
    )
    assert all(i["page_start"] is None for i in items)
    assert [i["fragment_key"] for i in items] == [f"F{n:06d}" for n in range(len(items))]
    clause = next(i for i in items if i["text"] == f.CONTRACT_PARAGRAPHS[0])
    assert clause["clause_ref"] == "1.1" and clause["heading_path"][-1] == "1. Предмет договора"
    # Канонический текст хранится отдельно от оригинала; смещения указывают в него.
    key = await api.scalar(
        "SELECT canonical_text_storage_key FROM document_versions WHERE id = :v", v=version_id
    )
    canonical = (storage(api).root / key).read_text(encoding="utf-8")
    for item in items:
        assert canonical[item["char_start"] : item["char_end"]] == item["text"]


async def test_docx_pages_from_libreoffice_render(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    # Страницы «рендера»: автонумерация видна как текст, ячейки таблицы — без разделителей.
    page1 = ["ДОГОВОР ПОСТАВКИ № 15", "г. Москва", "1. Предмет договора", *f.CONTRACT_PARAGRAPHS[:2]]
    page2 = [
        "№ Товар Цена, руб.",
        "1 Бумага А4 350",
        "2 Тонер 4 200",
        "Текст после таблицы.",
        "2. Цена",
        f.CONTRACT_PARAGRAPHS[2],
        "1. Первый пункт автонумерации",
        "2. Второй пункт автонумерации",
    ]
    pages = [page1, page2]
    converter = FakeConverter(tmp_path, pages)
    doc_id, version_id = await uploaded(
        api, client, f.contract_docx(tmp_path / "c.docx").read_bytes(), "c.docx"
    )
    assert await extract(api, version_id, converter) == "ready"
    assert converter.calls == [("docx", "pdf")]
    r = await client.get(f"/api/documents/{doc_id}/versions/{version_id}/pages")
    body = r.json()
    assert body["page_source"] == "libreoffice_render" and body["page_count"] == 2
    assert [p["page_no"] for p in body["items"]] == [1, 2]
    second_page = await fragments(client, doc_id, version_id, page=2)
    assert second_page["items"][0]["text"] == "№ | Товар | Цена, руб."
    version = (await client.get(f"/api/documents/{doc_id}/versions/{version_id}")).json()
    assert {"code": "pages_approximate"} in version["warnings"]
    assert stored_files(api) == sorted(stored_files(api))  # временный PDF рендера удалён
    assert not any(p.startswith("tmp/") for p in stored_files(api))


async def test_pdf_extraction_pages_and_fragment_api(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    pdf = f.text_pdf(
        tmp_path / "t.pdf", [f.CONTRACT_PARAGRAPHS[:2], ["Статья 2. Цена", f.CONTRACT_PARAGRAPHS[2]]]
    )
    doc_id, version_id = await uploaded(api, client, pdf.read_bytes(), "t.pdf")
    assert await extract(api, version_id) == "ready"
    pages = (await client.get(f"/api/documents/{doc_id}/versions/{version_id}/pages")).json()
    assert (pages["page_source"], pages["page_count"], len(pages["items"])) == ("pdf_native", 2, 2)
    page1 = await fragments(client, doc_id, version_id, page=1)
    assert [i["clause_ref"] for i in page1["items"]] == ["1.1", "1.2"] and page1["total"] == 2
    first = await fragments(client, doc_id, version_id, limit=3)
    assert len(first["items"]) == 3 and first["next_after_seq"] == 2 and first["total"] == 4
    rest = await fragments(client, doc_id, version_id, after_seq=first["next_after_seq"])
    assert [i["seq"] for i in rest["items"]] == [3] and rest["next_after_seq"] is None


@pytest.mark.parametrize(
    ("factory", "name", "status", "code"),
    [
        (lambda t: f.scanned_pdf(t / "s.pdf"), "scan.pdf", "unsupported_no_text", "no_text_layer"),
        (lambda t: f.empty_docx(t / "e.docx"), "empty.docx", "unsupported_no_text", "no_text"),
        (lambda t: f.encrypted_pdf(t / "e.pdf"), "enc.pdf", "failed", "encrypted_document"),
        (
            lambda t: f.write(t / "c.pdf", b"%PDF-1.7\n1 0 obj << garbage"),
            "bad.pdf",
            "failed",
            "corrupted_file",
        ),
        (lambda t: f.xxe_docx(t / "x.docx"), "xxe.docx", "failed", "corrupted_file"),
    ],
)
async def test_extraction_failures_are_recorded(
    api: ApiEnv,
    analyst: tuple[User, ApiClient],
    tmp_path: Path,
    factory: Any,
    name: str,
    status: str,
    code: str,
) -> None:
    _, client = analyst
    doc_id, version_id = await uploaded(api, client, factory(tmp_path).read_bytes(), name)
    assert await extract(api, version_id) == status
    version = (await client.get(f"/api/documents/{doc_id}/versions/{version_id}")).json()
    assert (version["extraction_status"], version["extraction_error_code"]) == (status, code)
    assert version["fragment_count"] == 0
    assert await api.scalar("SELECT count(*) FROM document_fragments") == 0


async def test_too_many_pages(api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path) -> None:
    _, client = analyst
    api.settings.max_pages = 3
    _, version_id = await uploaded(api, client, f.many_pages_pdf(tmp_path / "m.pdf", 5).read_bytes(), "m.pdf")
    assert await extract(api, version_id) == "failed"
    assert await api.scalar("SELECT extraction_error_code FROM document_versions") == "too_many_pages"


async def test_doc_converted_through_converter(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    data = (f.FIXTURES / "contract.doc").read_bytes()
    doc_id, version_id = await uploaded(api, client, data, "Старый договор.doc")
    converter = FakeConverter(tmp_path)
    assert await extract(api, version_id, converter) == "ready"
    assert converter.calls == [("doc", "docx"), ("docx", "pdf")]
    version = (await client.get(f"/api/documents/{doc_id}/versions/{version_id}")).json()
    assert version["detected_format"] == "doc" and version["converted_from_doc"]
    assert {"code": "converted_from_doc"} in version["warnings"]
    assert any(p.startswith("converted/") for p in stored_files(api))
    texts = [i["text"] for i in (await fragments(client, doc_id, version_id))["items"]]
    assert f.CONTRACT_PARAGRAPHS[0] in texts


async def test_converter_outage_retries_then_fails_doc(api: ApiEnv, analyst: tuple[User, ApiClient]) -> None:
    _, client = analyst
    doc_id, version_id = await uploaded(api, client, (f.FIXTURES / "contract.doc").read_bytes(), "a.doc")
    with pytest.raises(RetryLater):
        await extract(api, version_id, last=False)
    version = (await client.get(f"/api/documents/{doc_id}/versions/{version_id}")).json()
    assert (version["extraction_status"], version["extraction_error_code"]) == (
        "pending",
        "converter_unavailable",
    )
    assert await extract(api, version_id, last=True) == "failed"
    # Повтор вручную: failed → pending + новая задача.
    r = await client.post(f"/api/documents/{doc_id}/versions/{version_id}/retry-extraction")
    assert r.status_code == 202 and r.json()["extraction_status"] == "pending"
    r = await client.post(f"/api/documents/{doc_id}/versions/{version_id}/retry-extraction")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "extraction_not_failed")


async def test_extraction_of_deleted_document_is_skipped(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    doc_id, version_id = await uploaded(
        api, client, f.text_pdf(tmp_path / "t.pdf", [["Текст"]]).read_bytes(), "t.pdf"
    )
    assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 202
    assert await extract(api, version_id) == "skipped"
    assert await api.scalar("SELECT count(*) FROM document_fragments") == 0


# ---------------------------------------------------------------- оригинал и удаление


async def test_download_original_is_attachment(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path
) -> None:
    _, client = analyst
    data = f.contract_docx(tmp_path / "c.docx").read_bytes()
    doc_id, version_id = await uploaded(api, client, data, 'Договор №1 "Ромашка".docx')
    r = await client.get(f"/api/documents/{doc_id}/versions/{version_id}/file")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == DOCX_MIME
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition
    assert '"' not in disposition.split("filename=", 1)[1].split(";")[0].strip('"')
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in r.headers["cache-control"]
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'document.file_downloaded'") == 1


async def test_delete_and_purge(api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path) -> None:
    _, client = analyst
    doc_id, version_id = await uploaded(
        api, client, f.contract_docx(tmp_path / "c.docx").read_bytes(), "c.docx"
    )
    await extract(api, version_id)
    assert len(stored_files(api)) == 2  # оригинал + канонический текст
    r = await client.delete(f"/api/documents/{doc_id}")
    assert r.status_code == 202 and r.json()["status"] == "deleting"
    # Сразу исчезает из выдачи.
    assert (await client.get(f"/api/documents/{doc_id}")).status_code == 404
    assert (await client.get("/api/documents")).json()["items"] == []
    jobs = await api.rows("SELECT type FROM jobs WHERE type = 'purge_document'")
    assert len(jobs) == 1
    assert await purge_document_files_and_rows(api.factory, storage(api), uuid.UUID(doc_id))
    assert stored_files(api) == []
    for table in ("documents", "document_versions", "document_fragments"):
        assert await api.scalar(f"SELECT count(*) FROM {table}") == 0
    audit = await api.rows("SELECT details FROM audit_log WHERE action = 'document.deleted'")
    assert len(audit) == 1 and "sha256" in audit[0][0]
    # Повторный запуск задачи безопасен.
    assert not await purge_document_files_and_rows(api.factory, storage(api), uuid.UUID(doc_id))


# ---------------------------------------------------------------- изоляция доступа


async def test_idor_on_document_version_text_and_file(api: ApiEnv, tmp_path: Path) -> None:
    owner = await api.create_user(UserRole.analyst)
    owner_client = await api.login(owner)
    doc_id, version_id = await uploaded(
        api,
        owner_client,
        f.text_pdf(tmp_path / "t.pdf", [["Конфиденциальный текст договора"]]).read_bytes(),
        "t.pdf",
    )
    await extract(api, version_id)
    other_doc, other_version = await uploaded(
        api, owner_client, f.text_pdf(tmp_path / "o.pdf", [["Другой договор"]]).read_bytes(), "o.pdf"
    )
    paths = [
        f"/api/documents/{doc_id}",
        f"/api/documents/{doc_id}/versions/{version_id}",
        f"/api/documents/{doc_id}/versions/{version_id}/pages",
        f"/api/documents/{doc_id}/versions/{version_id}/fragments",
        f"/api/documents/{doc_id}/versions/{version_id}/file",
    ]
    for role in (UserRole.analyst, UserRole.admin, UserRole.viewer):
        stranger = await api.login(await api.create_user(role))
        for path in paths:
            r = await stranger.get(path)
            assert r.status_code == 404, (role, path)
            assert "Конфиденциальный" not in r.text
        assert (await stranger.delete(f"/api/documents/{doc_id}")).status_code == 404
        assert (await stranger.get(f"/api/documents/{doc_id}/access")).status_code == 404
        assert (await stranger.get("/api/documents")).json()["items"] == []
    # Версия другого документа под чужим document_id — 404 даже для владельца.
    for path in (
        f"/api/documents/{doc_id}/versions/{other_version}",
        f"/api/documents/{other_doc}/versions/{version_id}/fragments",
        f"/api/documents/{doc_id}/versions/{other_version}/file",
    ):
        assert (await owner_client.get(path)).status_code == 404
    assert (await owner_client.get(f"/api/documents/{uuid.uuid4()}")).status_code == 404


async def test_shared_access_and_superadmin_audit(api: ApiEnv, tmp_path: Path) -> None:
    owner = await api.create_user(UserRole.analyst)
    owner_client = await api.login(owner)
    viewer = await api.create_user(UserRole.viewer)
    viewer_client = await api.login(viewer)
    colleague = await api.create_user(UserRole.analyst)
    colleague_client = await api.login(colleague)
    doc_id, version_id = await uploaded(
        api, owner_client, f.text_pdf(tmp_path / "t.pdf", [["Текст договора"]]).read_bytes(), "t.pdf"
    )
    await extract(api, version_id)

    r = await owner_client.put(
        f"/api/documents/{doc_id}/access", json={"email": viewer.email, "permission": "analyze"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "viewer_cannot_analyze")
    r = await owner_client.put(
        f"/api/documents/{doc_id}/access", json={"email": viewer.email, "permission": "view"}
    )
    assert r.status_code == 200 and [i["email"] for i in r.json()["items"]] == [viewer.email]
    r = await owner_client.put(
        f"/api/documents/{doc_id}/access", json={"email": colleague.email, "permission": "analyze"}
    )
    assert r.status_code == 200
    r = await owner_client.put(
        f"/api/documents/{doc_id}/access", json={"email": "nobody@example.org", "permission": "view"}
    )
    assert r.status_code == 404

    listed = (await viewer_client.get("/api/documents")).json()["items"]
    assert [d["id"] for d in listed] == [doc_id]
    assert listed[0]["access"] == "view" and not listed[0]["can_manage"] and not listed[0]["can_analyze"]
    assert listed[0]["owner_display_name"] == owner.display_name
    assert (
        await viewer_client.get(f"/api/documents/{doc_id}/versions/{version_id}/fragments")
    ).status_code == 200
    assert (await viewer_client.get(f"/api/documents/{doc_id}/versions/{version_id}/file")).status_code == 200
    # Доступ на просмотр не даёт права удалить или делиться.
    assert (await viewer_client.delete(f"/api/documents/{doc_id}")).status_code == 404
    colleague_view = (await colleague_client.get(f"/api/documents/{doc_id}")).json()
    assert colleague_view["can_analyze"] and not colleague_view["can_share"]
    r = await colleague_client.put(
        f"/api/documents/{doc_id}/access", json={"email": viewer.email, "permission": "view"}
    )
    assert r.status_code == 404
    assert (await colleague_client.post(
        f"/api/documents/{doc_id}/versions/{version_id}/retry-extraction"
    )).status_code == 404  # fmt: skip

    r = await owner_client.delete(f"/api/documents/{doc_id}/access/{viewer.id}")
    assert r.status_code == 200
    assert (await viewer_client.get(f"/api/documents/{doc_id}")).status_code == 404

    superadmin = await api.login(await api.create_user(UserRole.superadmin))
    assert (await superadmin.get("/api/documents")).json()["items"] == []  # в списке — только свои
    assert (
        await superadmin.get(f"/api/documents/{doc_id}/versions/{version_id}/fragments")
    ).status_code == 200
    r = await superadmin.put(
        f"/api/documents/{doc_id}/access", json={"email": viewer.email, "permission": "view"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (403, "not_document_owner")
    actions = [a for (a,) in await api.rows("SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("document.access_granted") == 2
    assert actions.count("document.access_revoked") == 1
    assert "document.accessed_by_superadmin" in actions


async def test_document_text_is_not_logged(
    api: ApiEnv, analyst: tuple[User, ApiClient], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, client = analyst
    secret = "Уникальнаяфразадоговора"
    doc_id, version_id = await uploaded(
        api, client, f.text_pdf(tmp_path / "t.pdf", [[f"{secret} и обычный текст"]]).read_bytes(), "Тайна.pdf"
    )
    await extract(api, version_id)
    await fragments(client, doc_id, version_id)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "Тайна" not in captured.out + captured.err
    audit = await api.rows("SELECT details::text FROM audit_log")
    assert all(secret not in row[0] and "Тайна" not in row[0] for row in audit)


async def test_upload_limits_endpoint(api: ApiEnv) -> None:
    analyst = await api.login(await api.create_user(UserRole.analyst))
    viewer = await api.login(await api.create_user(UserRole.viewer))
    body = (await analyst.get("/api/documents/upload-limits")).json()
    assert body == {
        "max_upload_mb": 25,
        "max_pages": 500,
        "extensions": ["doc", "docx", "pdf"],
        "can_upload": True,
    }
    assert (await viewer.get("/api/documents/upload-limits")).json()["can_upload"] is False
    assert (await api.client().get("/api/documents/upload-limits")).status_code == 401
