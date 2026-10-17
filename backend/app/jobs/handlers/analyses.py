"""Задача анализа договора (`run_analysis`): см. `app.analysis.runner`."""

from __future__ import annotations

import uuid
from typing import Any

from app.analysis.runner import AnalysisRunner
from app.jobs.registry import JobContext, JobError, register
from app.services.analyses import RUN_ANALYSIS_JOB


@register(RUN_ANALYSIS_JOB)
async def run_analysis(ctx: JobContext, payload: dict[str, Any]) -> None:
    try:
        analysis_id = uuid.UUID(str(payload["analysis_id"]))
    except (KeyError, ValueError) as exc:
        raise JobError("invalid_payload", retryable=False) from exc
    if ctx.llm_factory is None:
        raise JobError("llm_factory_missing", retryable=False)
    runner = AnalysisRunner(
        ctx.settings, ctx.session_factory, ctx.llm_factory, analysis_id, last_attempt=ctx.is_last_attempt
    )
    await runner.run()
