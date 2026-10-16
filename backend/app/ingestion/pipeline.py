"""Конвейер извлечения версии документа (выполняется задачей `extract_document` в worker).

pending → processing → ready | unsupported_no_text | failed.

1. DOC → DOCX через converter (результат сохраняется как `converted_storage_key`).
2. Разбор PDF/DOCX в дочернем процессе с лимитами (`sandbox`).
3. DOCX без надёжной разметки Word → рендер в PDF через converter и сопоставление со страницами;
   если не удалось — страницы не указываются (предупреждение).
4. Сборка канонического текста, фрагментов и страниц; запись файла текста и строк в одной транзакции.

Недоступность converter — временная ошибка (задача повторяется); на последней попытке
версия получает `failed` (DOC) или остаётся без страниц (DOCX).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.ingestion import EXTRACTOR_VERSION
from app.ingestion.assemble import Assembled, assemble
from app.ingestion.converter_client import ConverterClient, ConverterUnavailable
from app.ingestion.model import ExtractionFailed, NoTextLayer, RawExtraction, warning
from app.ingestion.pagemap import map_fragments_to_pages
from app.ingestion.sandbox import SandboxLimits, run_sandbox
from app.logging import get_logger
from app.models.documents import Document, DocumentFragment, DocumentPage, DocumentVersion
from app.models.enums import DocumentFormat, DocumentStatus, ExtractionStatus, FragmentKind, PageSource
from app.storage import LocalStorage

log = get_logger(__name__)

_INSERT_BATCH = 1000
_EXTRACTION_WARNING_CODES_FROM_UPLOAD = {"doc_macros_ignored"}


class RetryLater(Exception):
    """Временная ошибка: задачу нужно повторить (версия возвращена в pending)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Skip(Exception):
    """Документ удалён или версия изменена другим процессом — результат не сохраняется."""


@dataclass
class _Outcome:
    raw: RawExtraction
    assembled: Assembled
    warnings: list[dict[str, Any]]


@dataclass
class _Source:
    format: DocumentFormat
    original_key: str
    converted_key: str | None
    upload_warnings: list[dict[str, Any]]


class ExtractionPipeline:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        storage: LocalStorage,
        converter: ConverterClient | None = None,
    ) -> None:
        self.factory = session_factory
        self.storage = storage
        self.converter = converter or ConverterClient(
            settings.converter_url, settings.converter_timeout_seconds
        )
        self.limits = SandboxLimits(
            timeout_seconds=settings.extraction_timeout_seconds,
            memory_mb=settings.extraction_memory_mb,
            max_pages=settings.max_pages,
        )

    async def run(self, version_id: uuid.UUID, *, last_attempt: bool = True) -> str:
        """Возвращает итоговый статус версии или `skipped`. Бросает RetryLater для повтора задачи."""
        source = await self._start(version_id)
        if source is None:
            return "skipped"
        started = time.perf_counter()
        try:
            outcome = await self._extract(version_id, source, last_attempt=last_attempt)
        except _Skip:
            return "skipped"
        except NoTextLayer as exc:
            status = await self._finish(version_id, ExtractionStatus.unsupported_no_text, exc.code)
        except ExtractionFailed as exc:
            status = await self._finish(version_id, ExtractionStatus.failed, exc.code)
        except ConverterUnavailable as exc:
            if last_attempt:
                status = await self._finish(version_id, ExtractionStatus.failed, exc.code)
            else:
                await self._finish(version_id, ExtractionStatus.pending, exc.code)
                raise RetryLater(exc.code) from exc
        except Exception:
            if last_attempt:
                await self._finish(version_id, ExtractionStatus.failed, "internal_error")
            raise
        else:
            status = await self._save(version_id, source, outcome)
        log.info(
            "extraction_finished",
            version_id=str(version_id),
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return status

    # ---------------------------------------------------------------- шаги

    async def _locked_version(self, db: AsyncSession, version_id: uuid.UUID) -> DocumentVersion | None:
        row = (
            await db.execute(
                select(DocumentVersion, Document.status)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(DocumentVersion.id == version_id)
                .with_for_update(of=DocumentVersion)
            )
        ).first()
        if row is None or row[1] is not DocumentStatus.active:
            return None
        version: DocumentVersion = row[0]
        return version

    async def _start(self, version_id: uuid.UUID) -> _Source | None:
        async with self.factory() as db:
            version = await self._locked_version(db, version_id)
            if version is None or version.extraction_status not in (
                ExtractionStatus.pending,
                ExtractionStatus.processing,  # предыдущая попытка прервана (worker упал)
            ):
                return None
            version.extraction_status = ExtractionStatus.processing
            version.extraction_error_code = None
            source = _Source(
                format=version.detected_format,
                original_key=version.storage_key,
                converted_key=version.converted_storage_key,
                upload_warnings=[
                    w for w in version.warnings if w.get("code") in _EXTRACTION_WARNING_CODES_FROM_UPLOAD
                ],
            )
            await db.commit()
        return source

    async def _extract(self, version_id: uuid.UUID, source: _Source, *, last_attempt: bool) -> _Outcome:
        warnings: list[dict[str, Any]] = []
        path = self.storage.local_path(source.original_key)
        mode = "extract-pdf"
        if source.format is DocumentFormat.doc:
            converted_key = source.converted_key
            if converted_key is None or not self.storage.exists(converted_key):
                converted_key = await self._convert_doc(version_id, path)
            path = self.storage.local_path(converted_key)
            mode = "extract-docx"
            warnings.append(warning("converted_from_doc"))
        elif source.format is DocumentFormat.docx:
            mode = "extract-docx"
        raw = RawExtraction.from_json(await asyncio.to_thread(run_sandbox, mode, path, self.limits))
        if raw.needs_page_render:
            await self._render_pages(raw, path, warnings, last_attempt=last_attempt)
        assembled = assemble(raw)
        if not assembled.fragments:
            raise NoTextLayer("no_text")
        return _Outcome(raw=raw, assembled=assembled, warnings=warnings + raw.warnings)

    async def _convert_doc(self, version_id: uuid.UUID, path: Any) -> str:
        tmp_key = self.storage.new_key("tmp")
        converted_key = self.storage.new_key("converted")
        try:
            await self.converter.convert(path, "doc", "docx", self.storage.output_path(tmp_key))
            self.storage.move(tmp_key, converted_key)
        finally:
            self.storage.delete(tmp_key)
        async with self.factory() as db:
            version = await self._locked_version(db, version_id)
            if version is None:
                self.storage.delete(converted_key)
                raise _Skip()
            old = version.converted_storage_key
            version.converted_storage_key = converted_key
            await db.commit()
        if old and old != converted_key:
            self.storage.delete(old)
        return converted_key

    async def _render_pages(
        self, raw: RawExtraction, docx_path: Any, warnings: list[dict[str, Any]], *, last_attempt: bool
    ) -> None:
        tmp_key = self.storage.new_key("tmp")
        try:
            await self.converter.convert(docx_path, "docx", "pdf", self.storage.output_path(tmp_key))
            data = await asyncio.to_thread(
                run_sandbox, "pdf-page-texts", self.storage.local_path(tmp_key), self.limits
            )
        except ConverterUnavailable as exc:
            if not last_attempt:
                raise
            warnings.append(warning("pages_unavailable", reason=exc.code))
            return
        except ExtractionFailed as exc:
            if exc.code == "too_many_pages":
                raise
            warnings.append(warning("pages_unavailable", reason=exc.code))
            return
        finally:
            self.storage.delete(tmp_key)
        page_texts = [str(t) for t in data["pages"]]
        unmapped = map_fragments_to_pages(raw.fragments, page_texts)
        raw.page_source = PageSource.libreoffice_render.value
        raw.page_count = len(page_texts)
        warnings.append(warning("pages_approximate"))
        if unmapped:
            warnings.append(warning("pages_unmapped", count=unmapped))

    async def _finish(self, version_id: uuid.UUID, status: ExtractionStatus, code: str) -> str:
        async with self.factory() as db:
            version = await self._locked_version(db, version_id)
            if version is None:
                return "skipped"
            version.extraction_status = status
            version.extraction_error_code = code[:64]
            await db.commit()
        return status.value

    async def _save(self, version_id: uuid.UUID, source: _Source, outcome: _Outcome) -> str:
        assembled = outcome.assembled
        text_key = self.storage.new_key("text")
        await asyncio.to_thread(self.storage.write_bytes, text_key, assembled.canonical_text.encode("utf-8"))
        old_text_key: str | None = None
        try:
            async with self.factory() as db:
                version = await self._locked_version(db, version_id)
                if version is None or version.extraction_status is not ExtractionStatus.processing:
                    raise _Skip()
                old_text_key = version.canonical_text_storage_key
                await db.execute(delete(DocumentPage).where(DocumentPage.version_id == version_id))
                await db.execute(delete(DocumentFragment).where(DocumentFragment.version_id == version_id))
                rows = [_fragment_row(version_id, f) for f in assembled.fragments]
                for i in range(0, len(rows), _INSERT_BATCH):
                    await db.execute(insert(DocumentFragment), rows[i : i + _INSERT_BATCH])
                page_source = PageSource(outcome.raw.page_source) if outcome.raw.page_source else None
                if assembled.pages and page_source is not None:
                    await db.execute(
                        insert(DocumentPage),
                        [
                            {
                                "id": uuid.uuid4(),
                                "version_id": version_id,
                                "page_no": p.page_no,
                                "char_start": p.char_start,
                                "char_end": p.char_end,
                                "source": page_source,
                            }
                            for p in assembled.pages
                        ],
                    )
                version.extraction_status = ExtractionStatus.ready
                version.extraction_error_code = None
                version.extractor_version = EXTRACTOR_VERSION
                version.page_count = assembled.page_count if page_source is not None else None
                version.page_source = page_source
                version.canonical_text_storage_key = text_key
                version.warnings = _dedupe(source.upload_warnings + outcome.warnings)
                await db.commit()
        except _Skip:
            self.storage.delete(text_key)
            return "skipped"
        except BaseException:
            self.storage.delete(text_key)
            raise
        if old_text_key and old_text_key != text_key:
            self.storage.delete(old_text_key)
        return ExtractionStatus.ready.value


def _fragment_row(version_id: uuid.UUID, f: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "version_id": version_id,
        "seq": f.seq,
        "fragment_key": f.fragment_key,
        "kind": FragmentKind(f.kind),
        "text": f.text,
        "text_sha256": f.text_sha256,
        "char_start": f.char_start,
        "char_end": f.char_end,
        "page_start": f.page_start,
        "page_end": f.page_end,
        "clause_ref": f.clause_ref[:64] if f.clause_ref else None,
        "heading_path": f.heading_path,
        "table_ref": f.table_ref,
        "meta": f.meta,
    }


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        code = str(item.get("code"))
        if code not in seen:
            seen.add(code)
            result.append(item)
    return result
