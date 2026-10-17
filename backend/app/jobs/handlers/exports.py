"""Задача формирования файла экспорта (`build_export`)."""

from __future__ import annotations

import uuid
from typing import Any

from app.jobs.registry import JobContext, JobError, register
from app.services.exports import BUILD_EXPORT_JOB, build_export, mark_failed


@register(BUILD_EXPORT_JOB)
async def build_export_job(ctx: JobContext, payload: dict[str, Any]) -> None:
    try:
        export_id = uuid.UUID(str(payload["export_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("invalid_payload", retryable=False) from exc
    try:
        await build_export(ctx.session_factory, ctx.storage, ctx.settings, export_id)
    except Exception:
        if ctx.is_last_attempt:
            await mark_failed(ctx.session_factory, export_id, "export_failed")
        raise
