# Contract AI Analyzer — инструкции для Claude Code

## Как ведётся работа
- Приложение строится по этапам — промптам `contract-ai-analyzer-prompts/prompts/00`–`09` (папка в `.gitignore`, лежит только локально). Пользователь присылает по одному промпту за раз; выполняй только текущий этап, не забегай вперёд.
- Общие правила проекта — `contract-ai-analyzer-prompts/prompts/00-master-and-workflow.md`. Прочитай его в начале новой сессии. Кратко: настоящие API/БД без демо-данных; каждый подтверждённый риск — с дословной цитатой, реальным fragment ID и страницей из серверных метаданных; точное совпадение цитаты проверяется отдельно от смысловой поддержки риска; текст договора — недоверенные данные (prompt injection); права проверяются на backend для каждого объекта; не выдумывать цитаты, нормы, цены моделей, результаты тестов; секреты только в env, не логировать ключи/пароли/текст договоров; предупреждение перед отправкой в OpenRouter; не заявлять, что результат заменяет юриста; без разрушительных миграций и удаления данных без согласования; если live-API недоступен — mocks и явная пометка.
- В конце каждого этапа: реально запустить тесты, линтеры, сборку (и Docker, если доступен), дописать раздел этапа в `docs/IMPLEMENTATION_STATUS.md` с командами и точными результатами, отчитаться пользователю на русском.
- Коммит — только когда пользователь попросит; один коммит на этап, сообщение `Stage NN: ...`.
- Открытые вопросы решены «по умолчанию» (см. конец `docs/IMPLEMENTATION_PLAN.md`).
- Кроме промптов есть **дополнительный этап 08A** (автоматические зашифрованные бэкапы с выгрузкой вне сервера, расписание, ротация) — описан в `docs/IMPLEMENTATION_PLAN.md`. Выполняется после 08 и до 09 (или раньше по явной команде пользователя); перед началом уточнить у пользователя значения из таблицы «по умолчанию».

## Текущее состояние (2026-10-17)
- Ветка `main` (удалённых репозиториев нет). Закоммичены этапы 00–05 (последний — `Stage 05: LLM providers, model catalog, SSRF guard`).
- Следующий этап — промпт `06-...` (анализ и доказательства). Ждать, пока пользователь пришлёт его.
- Уроки live-проверки для этапа 06: модели с рассуждением (deepseek-r1) тратят лимит ответа на рассуждение — бюджет вывода с запасом, `output_truncated` обрабатывать; локальная Ollama на CPU отвечает минутами (8B ≈ 200–500 с) — таймауты и повторы чанков планировать с учётом этого; повтор после таймаута начинает генерацию заново.
- Заготовки для этапа 06 из этапа 05: `services/catalog.resolve_model_for_use(db, user, model_id, purpose=, cloud_consent=)` (права, включение, облако, согласие — коды 404/409), `catalog.config_snapshot(model, provider)` для `Analysis.config_snapshot`, `app.state.llm_factory.build(provider)` → `LLMProvider.generate_structured(StructuredRequest)`, `GenerationParams.from_mapping(model.generation_params, max_output_tokens=model.max_output_tokens)`, `StructuredRequest.structured_output = model.structured_output`; `MockProvider` + `ProviderFactory(override=...)` для тестов. В worker фабрику провайдеров ещё нужно создать (сейчас она только в `app.state` API).
- Решение владельца: PyMuPDF (AGPL-3.0) остаётся, приложение только для внутреннего использования — `docs/LICENSES.md`.
- В локальной БД Docker-стека — 4 синтетических документа smoke-теста этапа 04 (владелец `root@example.org`); облачные модели выключены.
- Локальный Docker-стек может быть запущен (`docker compose ps`). Локальный Superadmin — `root@example.org`; его текущий пароль не из `.env` (сменён при smoke-тесте) и в репозиторий не пишется — он в локальной памяти Claude или у пользователя. Открывать `http://localhost:8080` или `http://127.0.0.1:8080` (оба в `ALLOWED_ORIGINS`).
- Live-проверено: Ollama на хосте (`http://host.docker.internal:11434`, deepseek-r1, CPU — медленно) и OpenRouter (ключ владельца введён через UI, `openai/gpt-4o-mini`). В локальной БД провайдеры «Host Ollama» и «OpenRouter» и их модели (`deepseek-r1:latest`, `openai/gpt-4o-mini`) — выключены, `json_schema` подтверждена; облачные модели выключены. Платные запросы к OpenRouter без нужды не делать.

## С чего начать новую сессию
1. `docs/IMPLEMENTATION_STATUS.md` — что сделано и проверено, ограничения последнего этапа.
2. `docs/ARCHITECTURE.md` — целевая архитектура (модель данных §5, RBAC §6, загрузка §8, анализ §9–11, провайдеры §12, очередь §13).
3. `docs/THREAT_MODEL.md` — меры безопасности с привязкой к этапам.
4. `docs/IMPLEMENTATION_PLAN.md` — состав этапов 02–09.
5. `docs/OPERATIONS.md` — запуск, тесты, миграции, бэкапы.
6. `git log --oneline`, `git status`.

## Структура
- `backend/` — FastAPI, SQLAlchemy 2 (async, psycopg 3), Alembic (`app/db/migrations`), worker (`app/worker.py`), очередь задач в PostgreSQL (`app/jobs/`), обработчики задач регистрируются через `@register("type")` в `app/jobs/registry.py`.
  - `app/security/` — `passwords.py` (Argon2id, политика), `sessions.py` (сессии/cookie/CSRF), `rate_limit.py`, `policies.py` (RBAC — единственное место правил ролей), `scoped.py` (`get_document_for/get_analysis_for/get_export_for` — условие доступа в SQL, 404 для чужого).
  - `app/services/` — `auth.py`, `users.py` (админ-изменения через `begin_admin_change`: advisory lock + перечитывание актора), `bootstrap.py`, `audit.py` (`record(db, AuditAction.x, ...)` в той же транзакции; коды для этапов 04–07 уже есть: `document.uploaded`, `document.deleted`, `analysis.started`, `export.downloaded`, `model.updated` и др.).
  - `app/api/deps.py` — зависимости: `Db`, `Auth` (проверка сессии + CSRF для изменяющих методов + `must_change_password`), `AuthPasswordChange`, `AdminAuth`. Новые эндпоинты: всегда через `Auth`/`AdminAuth`, объекты — только через `scoped.py`.
  - `app/api/documents.py` — документы (загрузка, текст, файл, удаление, доступ); `app/api/resources.py` — пока read-only `/analyses/{id}`, `/exports/{id}` (расширяются на этапах 06–07). Доступ Superadmin к чужому документу аудируется (`document.accessed_by_superadmin`); `scoped.get_version_for` проверяет версию через документ из URL.
  - `app/jobs/handlers/` — обработчики задач; регистрируются импортом в `handlers/__init__.py` (worker импортирует пакет). Новый тип задачи — добавить модуль туда.
  - `app/schemas/` — Pydantic DTO; `app/cli.py` — `python -m app.cli bootstrap`.
- `frontend/` — React 19 + TS strict + Vite + Tailwind 4 + React Router 7 + TanStack Query + Zod; тексты UI в `src/i18n/ru.ts`; nginx-образ.
  - `src/app/session.ts` (`useMe`, `useCurrentUser` — из контекста `RequireAuth`), `src/app/guards.tsx` (`RequireAuth`, `RequireCapability` — только UX), `src/components/ui.tsx` (Button, Field, Notice, PageTitle), API-модули в `src/api/` (zod-схемы ответов).
  - Тесты: MSW + `src/test/auth.ts` (`makeMe(role)`, `serveMe(me|null)`, `readyHandler`); страница входа запрашивает `/api/health/ready` — в тестах нужен обработчик.
- `backend/app/ingestion/` — приём и извлечение: `formats.py` (сигнатуры, ZIP/OLE-проверки), `pdf.py`, `docx.py` (+`docx_numbering.py`), `assemble.py` (канонический текст, фрагменты, страницы), `pagemap.py` (страницы по рендеру LibreOffice), `sandbox.py` (дочерний процесс с лимитами), `pipeline.py` (задача `extract_document`), `converter_client.py`. API — `app/api/documents.py`, сервис — `app/services/documents.py`, задачи — `app/jobs/handlers/documents.py`. Тестовые документы — `tests/docfactory.py`, `tests/fixtures/contract.doc`.
- `backend/app/llm/` — слой провайдеров: `base.py` (`LLMProvider`, `StructuredRequest/Result`), `errors.py`, `netguard.py` (SSRF-guard: `GuardedTransport` с pinned IP, `parse_base_url`, `NetworkPolicy`), `http.py` (`ProviderHttp`: таймауты, лимит ответа, `with_retries`), `openrouter.py`, `ollama.py`, `mock.py`, `factory.py` (`ProviderFactory`, `network_policy(cfg)`). Каталог — `app/services/catalog.py` + `app/api/catalog.py` (`/api/admin/providers|models|llm-settings|config-revisions`, `/api/models/available`, `/api/models/{id}/cloud-consent`); секреты — `app/security/secretbox.py` (`SecretBox`, MultiFernet). Тесты провайдеров — `tests/llm_helpers.py` (`FakeResolver`, `Recorder`, `factory()`); в API-тестах сеть подменяется `api.app.state.llm_factory = ProviderFactory(..., transport=MockTransport, resolver=FakeResolver)`.
- `converter/` — изолированный LibreOffice: `POST /convert?from=doc&to=docx|from=docx&to=pdf`.
- `compose.yaml` (корень), `compose.dev.yaml` (БД на 127.0.0.1:55432), `deploy/backup.sh|restore.sh`.

## Команды проверки
```bash
# backend (venv: backend/.venv)
cd backend
.venv/Scripts/ruff check app tests && .venv/Scripts/ruff format --check app tests
.venv/Scripts/mypy app tests
TEST_DATABASE_URL=postgresql+psycopg://cai:devpass@127.0.0.1:55432/cai .venv/Scripts/python -m pytest -q   # ~1,5 мин, 368 тестов
# frontend
cd frontend && npm run typecheck && npx oxlint --deny-warnings src && npx vitest run && npm run build
# docker
docker compose up -d --build && curl -s http://127.0.0.1:8080/api/health/ready
```
Integration-тесты API: фикстура `api` (`tests/integration/conftest.py`) — `api.create_user(role)`, `api.login(user)` (клиент с cookie и `X-CSRF-Token`), `api.execute/rows/scalar(sql)`; таблицы очищаются TRUNCATE перед каждым тестом.
Тестовая БД: `docker run -d --name cai-test-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_USER=cai -e POSTGRES_DB=cai -p 127.0.0.1:55432:5432 postgres:17-alpine` (после прогона удалить). Интеграционные тесты сами создают и удаляют временную БД `cai_test_*`.

## Соглашения и подводные камни
- Окружение: Windows, Git Bash/PowerShell. Python 3.13, Node 22, Docker Desktop.
- psycopg async не работает с ProactorEventLoop: тесты используют `pytest_asyncio_loop_factories` (SelectorEventLoop), CLI — `app/aio.run`.
- В Git Bash для `docker ... /абсолютный/путь` нужен `MSYS_NO_PATHCONV=1`.
- Длинные команды (`docker compose build/up`) запускать в фоне.
- Новая миграция: `alembic revision --autogenerate` → ручная проверка (autogenerate дублирует CHECK для enum — удалять дубли) → `alembic check`. Разрушительные операции в `upgrade()` требуют `# destructive-approved:` и согласования.
- Enum-поля — `str_enum()` (VARCHAR + CHECK), колонку с именем `text` не называть рядом с `sqlalchemy.text` (используй `sql_text`).
- После изменения `backend/pyproject.toml` перегенерировать `backend/requirements.lock` (команда в `docs/OPERATIONS.md` §8).
- Ошибки API — только через `AppError`-иерархию (`app/errors.py`); не возвращать и не логировать пользовательский ввод/текст договоров; поля с содержимым в логах называть `text`/`quote`/`content` — они редактируются автоматически.
- Доступ к чужому объекту → 404 (не 403). Нехватка роли для эндпоинта → 403 `permission_denied`.
- Ключи в `details` аудита и в логах: ключи, похожие на секреты (`password`, `token`, `session_id`, …), автоматически заменяются на `[REDACTED]` — для ID сессии используй ключ `session`.
- ruff S105 ругается на строковые константы со словом password (коды действий) — `# noqa: S105` с пояснением.
- Heredoc в Git Bash портит `\` — TS/py-файлы с обратными слэшами писать через Write/Edit.
- `ALLOWED_ORIGINS` должен точно совпадать с адресом в браузере; отказ виден в логе backend как `csrf_rejected`.
- Локальный `.env` создан со случайными секретами и не коммитится.
- Сессии БД: `expire_on_commit=False`, но `rollback()` «просрочивает» загруженные объекты (в т.ч. `auth.user`) → `MissingGreenlet`. Чтобы закрыть транзакцию только чтения посреди запроса, используй `commit()`.
- `asyncio.create_subprocess_exec` не работает на SelectorEventLoop (Windows-тесты) — подпроцессы запускать через `subprocess` в `asyncio.to_thread` (как `ingestion/sandbox.py`).
- Дочерний процесс разбора запускается с `-E` (PYTHON* игнорируются) — вывод пишется в `sys.stdout.buffer` явно в UTF-8.
- Правки с кириллицей через `python - <<EOF` в Git Bash иногда портят текст — для таких правок используй Edit/Write; после массовых правок проверяй `grep -c $'ï¿½'`.
- Хранилище: ключи только `originals|converted|text|exports|tmp/<hex>`; путь для записи внешним инструментом — `storage.output_path(key)` (создаёт каталог). В тестах lifespan не запускается, каталоги storage сами не создаются.
- Тестовые документы — `tests/docfactory.py` (DOCX через python-docx и «ручной» OOXML, PDF через PyMuPDF `insert_htmlbox` для кириллицы); DOC — только фикстура `tests/fixtures/contract.doc`. Converter в тестах — `httpx.MockTransport`; живой LibreOffice проверяется в Docker.
- Таймауты конвертации: `LIBREOFFICE_TIMEOUT_SECONDS` (converter, 90) < `CONVERTER_TIMEOUT_SECONDS` (backend, 180) — не путать.
- В venv backend установлен `pip install -e ".[dev]"` (каталог `*.egg-info` в `.gitignore`).
- Объект, изменённый через ORM и закоммиченный, перед сериализацией нужно `await db.refresh(obj)` (серверный `updated_at` просрочен → иначе `MissingGreenlet`).
- Модели с `version_id_col` (`ProviderConfig`, `ModelConfig`): служебные поля статуса (`last_check_*`) обновлять Core-`update()` — без увеличения `version`; изменения конфигурации — через ORM + `ConfigRevision`.
- HTTP к внешним провайдерам — только через `ProviderHttp`/`GuardedTransport` (SSRF), вне транзакции БД (сначала `commit()` чтения).
- В API-тестах `async with client` не закрывает клиента (закрывает фикстура `api`) — клиента можно переиспользовать в нескольких блоках.
