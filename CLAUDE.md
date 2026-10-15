# Contract AI Analyzer — инструкции для Claude Code

## Как ведётся работа
- Приложение строится по этапам — промптам `contract-ai-analyzer-prompts/prompts/00`–`09` (папка в `.gitignore`, лежит только локально). Пользователь присылает по одному промпту за раз; выполняй только текущий этап, не забегай вперёд.
- Общие правила проекта — `contract-ai-analyzer-prompts/prompts/00-master-and-workflow.md`. Прочитай его в начале новой сессии. Кратко: настоящие API/БД без демо-данных; каждый подтверждённый риск — с дословной цитатой, реальным fragment ID и страницей из серверных метаданных; точное совпадение цитаты проверяется отдельно от смысловой поддержки риска; текст договора — недоверенные данные (prompt injection); права проверяются на backend для каждого объекта; не выдумывать цитаты, нормы, цены моделей, результаты тестов; секреты только в env, не логировать ключи/пароли/текст договоров; предупреждение перед отправкой в OpenRouter; не заявлять, что результат заменяет юриста; без разрушительных миграций и удаления данных без согласования; если live-API недоступен — mocks и явная пометка.
- В конце каждого этапа: реально запустить тесты, линтеры, сборку (и Docker, если доступен), дописать раздел этапа в `docs/IMPLEMENTATION_STATUS.md` с командами и точными результатами, отчитаться пользователю на русском.
- Коммит — только когда пользователь попросит; один коммит на этап, сообщение `Stage NN: ...`.
- Открытые вопросы решены «по умолчанию» (см. конец `docs/IMPLEMENTATION_PLAN.md`).
- Кроме промптов есть **дополнительный этап 08A** (автоматические зашифрованные бэкапы с выгрузкой вне сервера, расписание, ротация) — описан в `docs/IMPLEMENTATION_PLAN.md`. Выполняется после 08 и до 09 (или раньше по явной команде пользователя); перед началом уточнить у пользователя значения из таблицы «по умолчанию».

## С чего начать новую сессию
1. `docs/IMPLEMENTATION_STATUS.md` — что сделано и проверено, ограничения последнего этапа.
2. `docs/ARCHITECTURE.md` — целевая архитектура (модель данных §5, RBAC §6, загрузка §8, анализ §9–11, провайдеры §12, очередь §13).
3. `docs/THREAT_MODEL.md` — меры безопасности с привязкой к этапам.
4. `docs/IMPLEMENTATION_PLAN.md` — состав этапов 02–09.
5. `docs/OPERATIONS.md` — запуск, тесты, миграции, бэкапы.
6. `git log --oneline`, `git status`.

## Структура
- `backend/` — FastAPI, SQLAlchemy 2 (async, psycopg 3), Alembic (`app/db/migrations`), worker (`app/worker.py`), очередь задач в PostgreSQL (`app/jobs/`), обработчики задач регистрируются через `@register("type")` в `app/jobs/registry.py`.
- `frontend/` — React 19 + TS strict + Vite + Tailwind 4 + React Router 7 + TanStack Query + Zod; тексты UI в `src/i18n/ru.ts`; nginx-образ.
- `converter/` — изолированный LibreOffice (пока только `/health`; `/convert` → 501 до этапа 04).
- `compose.yaml` (корень), `compose.dev.yaml` (БД на 127.0.0.1:55432), `deploy/backup.sh|restore.sh`.

## Команды проверки
```bash
# backend (venv: backend/.venv)
cd backend
.venv/Scripts/ruff check app tests && .venv/Scripts/ruff format --check app tests
.venv/Scripts/mypy app tests
TEST_DATABASE_URL=postgresql+psycopg://<user>:<pw>@127.0.0.1:<port>/<db> .venv/Scripts/python -m pytest -q
# frontend
cd frontend && npm run typecheck && npx oxlint --deny-warnings src && npx vitest run && npm run build
# docker
docker compose up -d --build && curl -s http://127.0.0.1:8080/api/health/ready
```
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
- Доступ к чужому объекту → 404 (не 403).
- Локальный `.env` создан со случайными секретами и не коммитится.
