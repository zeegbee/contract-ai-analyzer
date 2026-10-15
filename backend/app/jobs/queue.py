"""Очередь задач в PostgreSQL.

- `enqueue` — постановка задачи (с необязательным `dedup_key` среди активных задач);
- `claim` — атомарный захват задачи через `FOR UPDATE SKIP LOCKED` с lease;
- `heartbeat` — продление lease во время выполнения;
- `complete` / `fail` — завершение; при ошибке — повтор с экспоненциальной задержкой или `dead`;
- `reap_expired` — возврат задач с истёкшим lease (упавший worker) в очередь.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus
from app.models.system import Job

_MAX_BACKOFF_S = 600


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    dedup_key: str | None = None,
    delay: timedelta | None = None,
) -> uuid.UUID | None:
    """Ставит задачу. Возвращает ID или None, если активная задача с тем же dedup_key уже есть.

    Вызывающий код отвечает за commit — так задача ставится атомарно вместе с бизнес-изменениями.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "type": job_type,
        "payload": payload or {},
        "priority": priority,
        "max_attempts": max_attempts,
        "dedup_key": dedup_key,
    }
    stmt = insert(Job).values(**values)
    if delay is not None:
        stmt = stmt.values(run_after=func.now() + delay)
    if dedup_key is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedup_key"],
            index_where=text("dedup_key IS NOT NULL AND status IN ('queued', 'running')"),
        )
    result = await session.execute(stmt.returning(Job.id))
    row = result.first()
    return row[0] if row else None


async def claim(
    session: AsyncSession, worker_id: str, lease_seconds: int, job_types: list[str] | None = None
) -> ClaimedJob | None:
    """Захватывает одну задачу и коммитит захват."""
    query = (
        select(Job.id)
        .where(Job.status == JobStatus.queued, Job.run_after <= func.now())
        .order_by(Job.priority, Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_types:
        query = query.where(Job.type.in_(job_types))
    job_id = (await session.execute(query)).scalar_one_or_none()
    if job_id is None:
        await session.rollback()
        return None
    lease = func.now() + timedelta(seconds=lease_seconds)
    row = (
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.running,
                attempts=Job.attempts + 1,
                locked_by=worker_id,
                lease_expires_at=lease,
                heartbeat_at=func.now(),
                started_at=func.now(),
            )
            .returning(Job.id, Job.type, Job.payload, Job.attempts, Job.max_attempts)
        )
    ).one()
    await session.commit()
    return ClaimedJob(id=row[0], type=row[1], payload=row[2], attempts=row[3], max_attempts=row[4])


async def heartbeat(session: AsyncSession, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
    """Продлевает lease. False — задача больше не принадлежит этому worker'у (её забрал reaper)."""
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.running)
        .values(
            heartbeat_at=func.now(),
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
        )
        .returning(Job.id)
    )
    await session.commit()
    return result.first() is not None


async def complete(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.running)
        .values(status=JobStatus.succeeded, finished_at=func.now(), lease_expires_at=None)
        .returning(Job.id)
    )
    await session.commit()
    return result.first() is not None


async def fail(
    session: AsyncSession, job_id: uuid.UUID, worker_id: str, error_code: str, *, retryable: bool = True
) -> JobStatus | None:
    """Фиксирует ошибку. Повторяет с backoff, пока не исчерпаны попытки; иначе `dead`."""
    job = (
        await session.execute(
            select(Job)
            .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.running)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        await session.rollback()
        return None
    job.last_error_code = error_code[:64]
    job.locked_by = None
    job.lease_expires_at = None
    if retryable and job.attempts < job.max_attempts:
        backoff = min(_MAX_BACKOFF_S, 5 * 2 ** (job.attempts - 1))
        job.status = JobStatus.queued
        job.run_after = func.now() + timedelta(seconds=backoff)
    else:
        job.status = JobStatus.dead if retryable else JobStatus.failed
        job.finished_at = func.now()
    status = job.status
    await session.commit()
    return status


async def reap_expired(session: AsyncSession) -> int:
    """Возвращает в очередь задачи с истёкшим lease; исчерпавшие попытки — в `dead`."""
    requeued = await session.execute(
        update(Job)
        .where(
            Job.status == JobStatus.running,
            Job.lease_expires_at < func.now(),
            Job.attempts < Job.max_attempts,
        )
        .values(
            status=JobStatus.queued,
            locked_by=None,
            lease_expires_at=None,
            last_error_code="lease_expired",
            run_after=func.now(),
        )
        .returning(Job.id)
    )
    dead = await session.execute(
        update(Job)
        .where(
            Job.status == JobStatus.running,
            Job.lease_expires_at < func.now(),
            Job.attempts >= Job.max_attempts,
        )
        .values(
            status=JobStatus.dead,
            locked_by=None,
            lease_expires_at=None,
            last_error_code="lease_expired",
            finished_at=func.now(),
        )
        .returning(Job.id)
    )
    count = len(requeued.all()) + len(dead.all())
    await session.commit()
    return count
