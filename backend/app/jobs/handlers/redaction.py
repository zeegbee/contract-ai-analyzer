"""Задача обнаружения данных для предпросмотра маскировки (`redaction_scan`)."""

from __future__ import annotations

import uuid
from typing import Any

from app.jobs.registry import JobContext, JobError, register
from app.security.secretbox import SecretBox
from app.services.redaction import SCAN_JOB, run_scan_job


@register(SCAN_JOB)
async def redaction_scan(ctx: JobContext, payload: dict[str, Any]) -> None:
    try:
        preview_id = uuid.UUID(str(payload["preview_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("invalid_payload", retryable=False) from exc
    box = ctx.llm_factory.secret_box if ctx.llm_factory else SecretBox(ctx.settings.secrets_encryption_keys)
    await run_scan_job(
        ctx.session_factory,
        box,
        preview_id,
        timeout_seconds=ctx.settings.redaction_scan_timeout_seconds,
        memory_mb=ctx.settings.redaction_scan_memory_mb,
        last_attempt=ctx.is_last_attempt,
    )
