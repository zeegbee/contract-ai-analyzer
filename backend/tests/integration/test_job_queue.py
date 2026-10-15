"""Очередь задач: захват, SKIP LOCKED, повторы, reaper, выполнение worker'ом."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.jobs import queue
from app.jobs.registry import JobContext, JobError, register
from app.models.enums import JobStatus
from app.models.system import Job
from app.worker import Worker

pytestmark = pytest.mark.integration

Factory = async_sessionmaker[AsyncSession]


async def _job(factory: Factory, job_id: uuid.UUID) -> Job:
    async with factory() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _enqueue(factory: Factory, job_type: str = "t", **kw: Any) -> uuid.UUID | None:
    async with factory() as s:
        job_id = await queue.enqueue(s, job_type, {"x": 1}, **kw)
        await s.commit()
    return job_id


async def test_enqueue_claim_complete(session_factory: Factory) -> None:
    job_id = await _enqueue(session_factory)
    assert job_id
    async with session_factory() as s:
        claimed = await queue.claim(s, "w1", 60)
    assert claimed and claimed.id == job_id and claimed.attempts == 1 and claimed.payload == {"x": 1}
    async with session_factory() as s:
        assert await queue.claim(s, "w2", 60) is None  # единственная задача уже захвачена
    async with session_factory() as s:
        assert await queue.complete(s, job_id, "w1")
    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.succeeded and job.finished_at is not None


async def test_dedup_key_among_active_jobs(session_factory: Factory) -> None:
    first = await _enqueue(session_factory, dedup_key="doc:1")
    assert first
    assert await _enqueue(session_factory, dedup_key="doc:1") is None
    async with session_factory() as s:
        claimed = await queue.claim(s, "w", 60)
        assert claimed
        await queue.complete(s, claimed.id, "w")
    assert await _enqueue(session_factory, dedup_key="doc:1") is not None  # после завершения — можно


async def test_skip_locked_gives_distinct_jobs(session_factory: Factory) -> None:
    ids = {await _enqueue(session_factory) for _ in range(2)}
    async with session_factory() as s1, session_factory() as s2:
        a = await queue.claim(s1, "w1", 60)
        b = await queue.claim(s2, "w2", 60)
    assert a and b and a.id != b.id and {a.id, b.id} == ids


async def test_fail_retries_then_dead(session_factory: Factory) -> None:
    job_id = await _enqueue(session_factory, max_attempts=2)
    assert job_id
    async with session_factory() as s:
        assert await queue.claim(s, "w", 60)
    async with session_factory() as s:
        assert await queue.fail(s, job_id, "w", "provider_timeout") == JobStatus.queued
    job = await _job(session_factory, job_id)
    assert job.last_error_code == "provider_timeout"
    # backoff: задача не доступна сразу
    async with session_factory() as s:
        assert await queue.claim(s, "w", 60) is None
    async with session_factory() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(run_after=Job.created_at))
        await s.commit()
        assert await queue.claim(s, "w", 60)
    async with session_factory() as s:
        assert await queue.fail(s, job_id, "w", "provider_timeout") == JobStatus.dead


async def test_non_retryable_failure(session_factory: Factory) -> None:
    job_id = await _enqueue(session_factory)
    assert job_id
    async with session_factory() as s:
        await queue.claim(s, "w", 60)
    async with session_factory() as s:
        assert await queue.fail(s, job_id, "w", "invalid_file", retryable=False) == JobStatus.failed


async def test_reaper_requeues_expired_lease(session_factory: Factory) -> None:
    job_id = await _enqueue(session_factory)
    assert job_id
    async with session_factory() as s:
        await queue.claim(s, "crashed-worker", 60)
        await s.execute(update(Job).where(Job.id == job_id).values(lease_expires_at=Job.created_at))
        await s.commit()
    async with session_factory() as s:
        assert await queue.reap_expired(s) == 1
    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.queued and job.locked_by is None and job.last_error_code == "lease_expired"
    # старый worker больше не может завершить задачу
    async with session_factory() as s:
        assert not await queue.complete(s, job_id, "crashed-worker")
        assert not await queue.heartbeat(s, job_id, "crashed-worker", 60)


_seen: list[dict[str, Any]] = []


@register("test.ok")
async def _ok_handler(ctx: JobContext, payload: dict[str, Any]) -> None:
    _seen.append(payload)


@register("test.bad_input")
async def _bad_handler(ctx: JobContext, payload: dict[str, Any]) -> None:
    raise JobError("bad_input", retryable=False)


@register("test.crash")
async def _crash_handler(ctx: JobContext, payload: dict[str, Any]) -> None:
    raise RuntimeError("boom")


async def _run_one(worker: Worker, factory: Factory) -> None:
    async with factory() as s:
        claimed = await queue.claim(s, worker.worker_id, 60)
    assert claimed
    await worker.execute(claimed)


async def test_worker_executes_handlers(session_factory: Factory, pg_settings: Settings) -> None:
    worker = Worker(pg_settings, session_factory)
    ok = await _enqueue(session_factory, "test.ok")
    await _run_one(worker, session_factory)
    assert ok and (await _job(session_factory, ok)).status == JobStatus.succeeded
    assert _seen[-1] == {"x": 1}

    bad = await _enqueue(session_factory, "test.bad_input")
    await _run_one(worker, session_factory)
    assert bad and (await _job(session_factory, bad)).status == JobStatus.failed

    crash = await _enqueue(session_factory, "test.crash")
    await _run_one(worker, session_factory)
    job = await _job(session_factory, crash)  # type: ignore[arg-type]
    assert job.status == JobStatus.queued and job.last_error_code == "internal_error"

    async with session_factory() as s:
        await s.execute(update(Job).where(Job.id == crash).values(status=JobStatus.succeeded))
        await s.commit()
    unknown = await _enqueue(session_factory, "test.unknown")
    await _run_one(worker, session_factory)
    job = await _job(session_factory, unknown)  # type: ignore[arg-type]
    assert job.status == JobStatus.failed and job.last_error_code == "unknown_job_type"


async def test_worker_heartbeat(session_factory: Factory, pg_settings: Settings) -> None:
    from app.worker import healthcheck

    worker = Worker(pg_settings, session_factory)
    assert healthcheck(pg_settings) == 1
    await worker._write_heartbeat()
    assert healthcheck(pg_settings) == 0
