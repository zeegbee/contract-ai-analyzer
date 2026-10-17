"""Фоновый worker: `python -m app.worker` (и `--healthcheck` для Docker)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket
import sys
import time
import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.aio import run
from app.config import ConfigurationError, Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.jobs import handlers, queue
from app.jobs.registry import JobContext, JobError, get_handler
from app.llm.factory import ProviderFactory
from app.logging import bind_context, clear_context, configure_logging, get_logger
from app.models.system import WorkerHeartbeat
from app.security.secretbox import SecretBox
from app.services.analyses import reconcile_orphans
from app.services.exports import expire_exports
from app.services.retention import apply_retention
from app.storage import LocalStorage

log = get_logger("app.worker")

# Регистрация обработчиков задач (импорт ради побочного эффекта @register).
_ = handlers

REAP_INTERVAL_S = 60
HEARTBEAT_STALE_S = 90


class Worker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        llm_factory: ProviderFactory | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        # Ключ провайдера расшифровывается только при создании клиента (app.llm.factory).
        self.llm_factory = llm_factory or ProviderFactory(SecretBox(settings.secrets_encryption_keys))
        self.storage = LocalStorage(settings.storage_dir)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        self.storage.ensure_layout()
        log.info("worker_started", worker_id=self.worker_id, concurrency=self.settings.worker_concurrency)
        slots = [asyncio.create_task(self._slot(i)) for i in range(self.settings.worker_concurrency)]
        housekeeping = asyncio.create_task(self._housekeeping())
        await self.stopping.wait()
        log.info("worker_stopping", worker_id=self.worker_id)
        # Слоты заканчивают текущую задачу; незавершённые подберёт reaper после истечения lease.
        await asyncio.gather(*slots, return_exceptions=True)
        housekeeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping
        log.info("worker_stopped", worker_id=self.worker_id)

    async def _housekeeping(self) -> None:
        last_reap = 0.0
        while not self.stopping.is_set():
            try:
                await self._write_heartbeat()
                if time.monotonic() - last_reap > REAP_INTERVAL_S:
                    async with self.session_factory() as session:
                        reaped = await queue.reap_expired(session)
                    if reaped:
                        log.warning("jobs_reaped", count=reaped)
                    async with self.session_factory() as session:
                        orphans = await reconcile_orphans(session)
                    if orphans:
                        log.warning("analyses_without_job_closed", count=orphans)
                    expired = await expire_exports(self.session_factory, self.storage)
                    if expired:
                        log.info("exports_expired", count=expired)
                    # Политика хранения документов (по умолчанию выключена — ничего не делает).
                    await apply_retention(self.session_factory)
                    last_reap = time.monotonic()
            except Exception as exc:
                log.warning("housekeeping_failed", exc_type=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=15)

    async def _write_heartbeat(self) -> None:
        async with self.session_factory() as session:
            stmt = insert(WorkerHeartbeat).values(worker_id=self.worker_id, version=self.settings.app_version)
            stmt = stmt.on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_id], set_={"last_seen_at": func.now()}
            )
            await session.execute(stmt)
            await session.commit()
        self.settings.worker_heartbeat_file.touch()

    async def _slot(self, index: int) -> None:
        while not self.stopping.is_set():
            job: queue.ClaimedJob | None = None
            try:
                async with self.session_factory() as session:
                    job = await queue.claim(session, self.worker_id, self.settings.job_lease_seconds)
            except Exception as exc:
                log.warning("job_claim_failed", slot=index, exc_type=type(exc).__name__)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self.stopping.wait(), timeout=self.settings.job_poll_interval_seconds
                    )
                continue
            await self.execute(job)

    async def execute(self, job: queue.ClaimedJob) -> None:
        clear_context()
        bind_context(job_id=str(job.id), job_type=job.type)
        handler = get_handler(job.type)
        if handler is None:
            log.error("job_unknown_type")
            async with self.session_factory() as session:
                await queue.fail(session, job.id, self.worker_id, "unknown_job_type", retryable=False)
            return
        ctx = JobContext(
            settings=self.settings,
            session_factory=self.session_factory,
            storage=self.storage,
            job_id=str(job.id),
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            llm_factory=self.llm_factory,
        )
        beat = asyncio.create_task(self._job_heartbeat(job))
        started = time.perf_counter()
        try:
            await handler(ctx, job.payload)
        except JobError as exc:
            log.warning("job_failed", code=exc.code, retryable=exc.retryable, attempt=job.attempts)
            async with self.session_factory() as session:
                await queue.fail(session, job.id, self.worker_id, exc.code, retryable=exc.retryable)
        except Exception as exc:
            log.error("job_crashed", exc_type=type(exc).__name__, attempt=job.attempts, exc_info=exc)
            async with self.session_factory() as session:
                await queue.fail(session, job.id, self.worker_id, "internal_error")
        else:
            async with self.session_factory() as session:
                await queue.complete(session, job.id, self.worker_id)
            log.info("job_succeeded", duration_ms=round((time.perf_counter() - started) * 1000))
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            clear_context()

    async def _job_heartbeat(self, job: queue.ClaimedJob) -> None:
        interval = max(5, self.settings.job_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.session_factory() as session:
                    owned = await queue.heartbeat(
                        session, job.id, self.worker_id, self.settings.job_lease_seconds
                    )
                if not owned:
                    log.warning("job_lease_lost")
                    return
            except Exception as exc:
                log.warning("job_heartbeat_failed", exc_type=type(exc).__name__)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = create_engine(settings)
    worker = Worker(settings, create_session_factory(engine))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # не поддерживается на Windows
            loop.add_signal_handler(sig, worker.stopping.set)
    try:
        await worker.run()
    finally:
        await engine.dispose()


def healthcheck(settings: Settings) -> int:
    try:
        age = time.time() - settings.worker_heartbeat_file.stat().st_mtime
    except FileNotFoundError:
        return 1
    return 0 if age < HEARTBEAT_STALE_S else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    try:
        get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)  # логирование ещё не настроено
        sys.exit(2)
    if args.healthcheck:
        sys.exit(healthcheck(get_settings()))
    run(_main())


if __name__ == "__main__":
    main()
