"""Конвейер анализа на реальном PostgreSQL со сценарной моделью (live-модель не используется):
полный прогон, длинный документ, ошибки модели и провайдера, повтор, отмена, восстановление."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from typing import Any

import pytest

from app.analysis.chunking import FragmentSize, plan_chunks
from app.analysis.runner import AnalysisRunner, Superseded
from app.jobs import queue
from app.llm.base import Usage
from app.llm.errors import InvalidModelOutput, ProviderAuthError, ProviderUnavailable
from app.models.enums import UserRole
from app.models.users import User
from app.services.analyses import reconcile_orphans
from tests.analysis_helpers import (
    CONTRACT,
    Doc,
    FakeLLM,
    accept_notice,
    by_fragment,
    confirmed_preview,
    enable_cloud,
    factory_for,
    get_analysis,
    key,
    make_document,
    make_model,
    risk,
    risk_list,
    risks,
    run_jobs,
    start,
    text_of,
    user_text,
)
from tests.integration.conftest import ApiClient, ApiEnv

UNILATERAL = "Исполнитель вправе в одностороннем порядке изменить стоимость услуг"


@pytest.fixture
async def setup(api: ApiEnv) -> tuple[User, ApiClient, Doc, Any]:
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    doc = await make_document(api, user)
    _provider, model = await make_model(api)
    return user, client, doc, model


def long_contract(n: int) -> list[tuple[str, str, str | None, int]]:
    items: list[tuple[str, str, str | None, int]] = []
    for i in range(n):
        section = i // 20 + 1
        if i % 20 == 0:
            items.append(("heading", f"Раздел {section}. Порядок взаимодействия", str(section), i // 40 + 1))
            continue
        body = (
            f"{section}.{i % 20}. Условие номер {i}: стороны согласовывают план работ, сроки выполнения "
            "этапов и порядок обмена документами, а также назначают ответственных лиц и уведомляют друг "
            "друга об изменениях в разумный срок."
        )
        items.append(("list_item", body, f"{section}.{i % 20}", i // 40 + 1))
    return items


async def long_setup(
    api: ApiEnv, n: int
) -> tuple[ApiClient, Doc, Any, list[tuple[str, str, str | None, int]]]:
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    contract = long_contract(n)
    doc = await make_document(api, user, contract)
    # Маленький контекст → много чанков.
    _provider, model = await make_model(api, context_window=8192, max_output_tokens=4096)
    return client, doc, model, contract


async def test_full_pipeline_verified_risk_report_and_snapshot(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    fake = FakeLLM(
        {"chunk_risks": by_fragment({key(6): [risk("Одностороннее изменение цены", [(key(6), UNILATERAL)])]})}
    )
    created = await start(client, doc, model)
    assert created["status"] == "queued"
    snapshot = created["config_snapshot"]
    assert snapshot["pipeline"]["prompts"]["chunk_analysis"]
    assert snapshot["pipeline"]["normalizer"] and snapshot["pipeline"]["chunker"]
    assert snapshot["model"]["external_model_id"] == model.external_model_id
    assert snapshot["verifier"] == {"same_as_analysis": True}
    assert "secret" not in str(snapshot) and "base_url" not in str(snapshot)

    assert await run_jobs(api, fake) == 1
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed", analysis
    summary = analysis["summary"]
    assert summary["party"]["code"] == "party_a"
    assert summary["party"]["resolved_name"] == "ООО «Альфа» (Заказчик)"
    assert summary["contract"]["contract_type"] == "договор оказания услуг"
    assert summary["model"]["display_name"] == model.display_name
    assert summary["counts"]["by_validation_status"] == {"verified_match": 1}
    assert "не заменяет консультацию юриста" in summary["disclaimer"]
    assert "безопас" not in summary["statement"]
    assert any("той же моделью" in line for line in summary["limitations"])
    coverage = analysis["coverage"]
    assert coverage["fragments_total"] == coverage["fragments_analyzed"] == len(CONTRACT)
    assert all(s["status"] == "analyzed" for s in coverage["sections"])
    assert coverage["page_source"] == "pdf_native" and coverage["pages_approximate"] is False
    assert analysis["usage"]["prompt_tokens"] > 0 and analysis["usage"]["calls"] >= 3
    assert analysis["progress"]["stage"] == "done"

    [item] = (await risk_list(client, created["id"]))["items"]
    assert item["risk_key"] == "R-001"
    assert item["validation_status"] == "verified_match"
    [evidence] = item["evidence"]
    assert evidence["match_status"] == "exact"
    assert evidence["fragment_key"] == key(6) and evidence["span_fragment_keys"] == [key(6)]
    assert (evidence["page_start"], evidence["page_end"]) == (1, 1)
    assert evidence["clause_ref"] == "2.2"
    assert evidence["relevance_status"] == "supports"
    canonical_start = sum(len(t) + 2 for _k, t, _c, _p in CONTRACT[:6])
    assert evidence["match_char_start"] == canonical_start + text_of(6).index(UNILATERAL)

    # Верификатор получил дословный текст документа в отдельном контексте (без истории анализа).
    [verify] = fake.by_schema("evidence_relevance")
    assert len(verify.messages) == 2 and UNILATERAL in user_text(verify)
    audit = await api.rows("SELECT details::text FROM audit_log WHERE action = 'analysis.started'")
    assert len(audit) == 1 and UNILATERAL not in audit[0][0]
    assert await api.scalar("SELECT status FROM jobs") == "succeeded"


async def test_completed_analysis_job_is_idempotent(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    fake = FakeLLM()
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    calls = len(fake.calls)
    # Повторная доставка той же задачи ничего не пересчитывает.
    runner = AnalysisRunner(
        api.settings, api.factory, factory_for(api, fake), uuid.UUID(created["id"]), last_attempt=True
    )
    await runner.run()
    assert len(fake.calls) == calls


async def test_long_document_chunks_overlap_stable_keys_and_dedup(api: ApiEnv) -> None:
    client, doc, model, contract = await long_setup(api, 400)
    created = await start(client, doc, model)
    chunks = await api.rows(
        "SELECT chunk_index, chunk_key, fragment_seq_start, fragment_seq_end, overlap_from_seq "
        "FROM analysis_chunks WHERE analysis_id = :a ORDER BY chunk_index",
        a=uuid.UUID(created["id"]),
    )
    assert len(chunks) > 10
    assert chunks[0][2] == 0 and chunks[-1][3] == len(contract) - 1
    for prev, cur in itertools.pairwise(chunks):
        assert cur[2] <= prev[3] < cur[3]  # перекрытие ≥ 1 фрагмента и движение вперёд
        assert cur[4] == cur[2]
    # Повторное планирование той же версии даёт те же ключи.
    budget = created["config_snapshot"]["chunking"]["budget_tokens"]
    sizes = [FragmentSize(i, len(t)) for i, (_k, t, _c, _p) in enumerate(contract)]
    assert [c.key for c in plan_chunks(doc.version_id, sizes, budget)] == [row[1] for row in chunks]

    # Риск во фрагменте перекрытия видят два соседних чанка — после дедупликации он один.
    overlap_seq = chunks[1][2]
    quote = text_of(overlap_seq, contract)[:80]
    same = risk("Неопределённый «разумный срок»", [(key(overlap_seq), quote)], category="deadlines")
    fake = FakeLLM({"chunk_risks": by_fragment({key(overlap_seq): [same]})})
    await run_jobs(api, fake)
    chunk_calls = fake.by_schema("chunk_risks")
    assert len(chunk_calls) == len(chunks)
    second = user_text(chunk_calls[1])
    assert f'<fragment id="{key(overlap_seq)}"' in second and 'context="previous"' in second
    assert f"Часть 2 из {len(chunks)}" in second
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed"
    assert analysis["coverage"]["fragments_analyzed"] == len(contract)
    assert analysis["coverage"]["chunks_done"] == len(chunks)
    [item] = (await risk_list(client, created["id"]))["items"]
    assert item["validation_notes"]["merged_from"] == 2
    assert item["dedup_group"] == "G-001"
    assert len(item["evidence"]) == 1


async def test_invalid_output_twice_fails_single_chunk_analysis(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    fake = FakeLLM({"chunk_risks": InvalidModelOutput(code="invalid_json")})
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    assert len(fake.by_schema("chunk_risks")) == 2  # исходный запрос + один repair
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed"
    assert analysis["error_code"] == "analysis_no_chunks_succeeded"
    assert analysis["error_message"] and "invalid" not in analysis["error_message"]
    assert analysis["can_retry"] is True
    assert await api.scalar("SELECT status FROM jobs") == "failed"


def truncated() -> InvalidModelOutput:
    """Обрыв по длине (зацикливание модели): токены ответа оплачены, хотя ответ отклонён."""
    return InvalidModelOutput(code="output_truncated").with_usage(
        Usage(prompt_tokens=900, completion_tokens=16384)
    )


async def test_truncated_output_retried_once_and_counted(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    fake = FakeLLM({"chunk_risks": [truncated(), truncated()]})
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    calls = fake.by_schema("chunk_risks")
    # Одна повторная попытка — с просьбой ответить короче; затем часть считается неудачной.
    assert len(calls) == 2
    assert "оборвался" in calls[1].messages[-1].content and "оборвался" not in user_text(calls[0])
    assert await api.scalar("SELECT error_code FROM analysis_chunks") == "output_truncated"
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed"
    # Расход оборванных ответов учтён (раньше терялся).
    assert analysis["usage"]["completion_tokens"] >= 2 * 16384


async def test_truncated_output_recovers_on_shorter_retry(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    found = risks(risk("Изменение цены", [(key(6), UNILATERAL)]))
    fake = FakeLLM({"chunk_risks": [truncated(), found], "cross_section_risks": [truncated(), risks()]})
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed", analysis
    assert analysis["coverage"]["cross_section"]["status"] == "done"
    assert len(fake.by_schema("cross_section_risks")) == 2
    assert [r["title"] for r in (await risk_list(client, created["id"]))["items"]] == ["Изменение цены"]


async def test_one_invalid_chunk_gives_completed_with_warnings(api: ApiEnv) -> None:
    client, doc, model, _contract = await long_setup(api, 60)
    fake = FakeLLM({"chunk_risks": [InvalidModelOutput(), InvalidModelOutput(), risks()]})
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed_with_warnings"
    coverage = analysis["coverage"]
    assert coverage["chunks_failed"][0]["index"] == 0
    assert coverage["chunks_failed"][0]["code"] == "invalid_model_output"
    assert coverage["fragments_analyzed"] < coverage["fragments_total"]
    assert any(s["status"] != "analyzed" for s in coverage["sections"])
    assert "chunks_failed" in analysis["summary"]["warnings"]


async def test_token_budget_exhaustion_skips_remaining_chunks(api: ApiEnv) -> None:
    client, doc, model, _contract = await long_setup(api, 60)
    created = await start(client, doc, model)
    await api.execute(
        "UPDATE analyses SET config_snapshot = "
        "jsonb_set(config_snapshot, '{limits,max_analysis_tokens}', '300')"
    )
    fake = FakeLLM()  # каждый вызов «стоит» 150 токенов
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed_with_warnings"
    assert analysis["coverage"]["chunks_skipped"]
    assert "chunks_skipped" in analysis["summary"]["warnings"]


async def test_transient_provider_error_retries_job_without_recomputing(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    fake = FakeLLM(
        {"chunk_risks": [ProviderUnavailable(), risks(risk("Изменение цены", [(key(6), UNILATERAL)]))]}
    )
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "running"
    assert analysis["progress"]["retry_scheduled"] == "provider_unavailable"
    assert await api.scalar("SELECT status FROM jobs") == "queued"
    assert await api.scalar("SELECT status FROM analysis_chunks") == "pending"
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed"
    assert len(fake.by_schema("contract_structure")) == 1  # структура сохранена после первой попытки
    assert len(fake.by_schema("chunk_risks")) == 2


async def test_transient_error_on_last_attempt_fails_with_safe_code(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    created = await start(client, doc, model)
    await api.execute("UPDATE jobs SET attempts = max_attempts - 1")
    fake = FakeLLM({"chunk_risks": ProviderUnavailable()})
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed" and analysis["error_code"] == "provider_unavailable"
    assert "временно недоступен" in analysis["error_message"]


async def test_fatal_provider_error_then_retry_endpoint(api: ApiEnv) -> None:
    client, doc, model, _contract = await long_setup(api, 60)
    fake = FakeLLM({"chunk_risks": [risks(), ProviderAuthError()]})
    created = await start(client, doc, model)
    total = created["progress"]["chunks_total"]
    assert total >= 2
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed" and analysis["error_code"] == "provider_auth_failed"
    assert await api.scalar("SELECT status FROM jobs") == "failed"
    assert await api.scalar("SELECT count(*) FROM analysis_chunks WHERE status = 'done'") == 1

    r = await client.post(f"/api/analyses/{created['id']}/retry", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    r = await client.post(f"/api/analyses/{created['id']}/retry", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "analysis_not_retryable"
    fake.handlers["chunk_risks"] = risks()
    calls_before = len(fake.by_schema("chunk_risks"))
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed"
    assert len(fake.by_schema("chunk_risks")) - calls_before == total - 1  # готовый чанк не пересчитан
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'analysis.retried'") == 1


async def test_cancel_queued_analysis(api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]) -> None:
    _, client, doc, model = setup
    created = await start(client, doc, model)
    r = await client.post(f"/api/analyses/{created['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert await api.scalar("SELECT status FROM jobs") == "cancelled"
    assert await run_jobs(api, FakeLLM()) == 0
    r = await client.post(f"/api/analyses/{created['id']}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "analysis_not_cancellable"
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'analysis.cancelled'") == 1


async def test_cancel_running_analysis_interrupts_model_call(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    api.settings.analysis_cancel_poll_seconds = 0.05
    created = await start(client, doc, model)
    interrupted: list[str] = []

    async def hook(req: Any) -> None:
        if req.schema_name != "chunk_risks":
            return
        try:
            # Отмена приходит во время «вызова модели»; worker может прервать его ещё до ответа API.
            r = await client.post(f"/api/analyses/{created['id']}/cancel")
            assert r.json()["status"] == "cancelling"
            await asyncio.Event().wait()  # «вызов модели» завершается только отменой
        except asyncio.CancelledError:
            interrupted.append(req.schema_name)
            raise

    fake = FakeLLM(hook=hook)
    await run_jobs(api, fake)
    assert interrupted == ["chunk_risks"]  # вызов модели прерван, а не дождались ответа
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "cancelled" and analysis["finished_at"]
    assert analysis["can_retry"] is True
    assert await api.scalar("SELECT status FROM analysis_chunks") == "pending"
    assert (
        await api.scalar("SELECT stage_results ? 'structure' FROM analyses") is True
    )  # результаты сохранены
    # После отмены — повтор продолжает с сохранённого места.
    r = await client.post(f"/api/analyses/{created['id']}/retry", json={})
    assert r.status_code == 200
    fake.hook = None
    await run_jobs(api, fake)
    assert (await get_analysis(client, created["id"]))["status"] == "completed"
    assert len(fake.by_schema("contract_structure")) == 1


async def test_recovery_after_worker_crash_and_lease_expiry(api: ApiEnv) -> None:
    client, doc, model, _contract = await long_setup(api, 60)
    created = await start(client, doc, model)
    calls = {"chunk": 0}

    async def hook(req: Any) -> None:
        if req.schema_name == "chunk_risks":
            calls["chunk"] += 1
            if calls["chunk"] == 2:
                raise asyncio.CancelledError  # процесс worker'а «упал» посреди второго чанка

    fake = FakeLLM(hook=hook)
    with pytest.raises(asyncio.CancelledError):
        await run_jobs(api, fake)
    assert await api.scalar("SELECT status FROM jobs") == "running"
    assert await api.scalar("SELECT count(*) FROM analysis_chunks WHERE status = 'done'") == 1
    await api.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
    async with api.factory() as session:
        assert await queue.reap_expired(session) == 1
    fake.hook = None
    before = len(fake.by_schema("chunk_risks"))
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "completed"
    total = analysis["progress"]["chunks_total"]
    assert len(fake.by_schema("chunk_risks")) - before == total - 1


async def test_stale_runner_cannot_write_after_takeover(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    created = await start(client, doc, model)
    analysis_id = uuid.UUID(created["id"])
    old = AnalysisRunner(
        api.settings, api.factory, factory_for(api, FakeLLM()), analysis_id, last_attempt=False
    )
    new = AnalysisRunner(
        api.settings, api.factory, factory_for(api, FakeLLM()), analysis_id, last_attempt=False
    )
    assert await old._start()
    assert await new._start()
    with pytest.raises(Superseded):
        async with old._tx():
            pass
    async with new._tx():
        pass


async def test_orphaned_analysis_is_closed(api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]) -> None:
    _, client, doc, model = setup
    created = await start(client, doc, model)
    await api.execute("UPDATE jobs SET status = 'dead'")
    async with api.factory() as db:
        assert await reconcile_orphans(db) == 0  # ещё в пределах grace-периода
    await api.execute("UPDATE analyses SET updated_at = now() - interval '10 minutes'")
    async with api.factory() as db:
        assert await reconcile_orphans(db) == 1
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed" and analysis["error_code"] == "job_lost"


async def test_cloud_disabled_during_analysis_stops_it(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    client = await api.login(user)
    doc = await make_document(api, user)
    provider, model = await make_model(api, cloud=True)
    await enable_cloud(api)
    await accept_notice(api, user, provider)

    async def hook(req: Any) -> None:
        if req.schema_name == "contract_structure":
            await enable_cloud(api, False)  # аварийное отключение облака администратором

    fake = FakeLLM(hook=hook)
    preview_id = await confirmed_preview(api, client, doc)
    created = await start(client, doc, model, cloud_consent=True, redaction_preview_id=preview_id)
    assert created["cloud_transfer"] is True and created["cloud_consent_at"]
    await run_jobs(api, fake)
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed" and analysis["error_code"] == "cloud_models_disabled"
    assert fake.by_schema("chunk_risks") == []


async def test_disabled_model_fails_queued_analysis(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any]
) -> None:
    _, client, doc, model = setup
    created = await start(client, doc, model)
    await api.execute("UPDATE model_configs SET enabled = false")
    await run_jobs(api, FakeLLM())
    analysis = await get_analysis(client, created["id"])
    assert analysis["status"] == "failed" and analysis["error_code"] == "model_disabled"


async def test_analysis_does_not_log_contract_text(
    api: ApiEnv, setup: tuple[User, ApiClient, Doc, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # caplog видит события до редактирования логов: проверяем, что текст договора вообще не
    # передаётся в логгер (capsys вывод structlog не перехватывает — поток задан при настройке).
    caplog.set_level("DEBUG")
    _, client, doc, model = setup
    fake = FakeLLM({"chunk_risks": risks(risk("Изменение цены", [(key(6), UNILATERAL)]))})
    created = await start(client, doc, model)
    await run_jobs(api, fake)
    await risk_list(client, created["id"])
    logs = caplog.text
    assert "analysis_completed" in logs and "analysis_chunk_done" in logs
    for fragment in (UNILATERAL, "ООО «Альфа»", "сопровождению информационной системы", "Изменение цены"):
        assert fragment not in logs
