# Contract AI Analyzer — статус реализации

Журнал ведётся по правилам `contract-ai-analyzer-prompts/prompts/00-master-and-workflow.md`.
Для каждого этапа: выполненные задачи, изменённые файлы, реально запущенные проверки (команда + точный результат), ограничения.

## Этап 00 — Общие правила (2026-10-15)

### Исследование репозитория
- Корень `D:\Temp\work\contract-ai-analyzer` не является Git-репозиторием.
- Кода приложения нет. Есть только набор промптов `contract-ai-analyzer-prompts/` (README, prompts 00–09, docs: IMPLEMENTATION_GUIDE, ACCEPTANCE_CHECKLIST, SECURITY_AND_PRIVACY).
- Сохранять из существующего кода нечего; стек выбирается из предпочтительного (React/TS/Vite + FastAPI/SQLAlchemy/Alembic + PostgreSQL + Docker Compose).

### Окружение разработки (проверено командами)
| Инструмент | Версия |
|---|---|
| Python | 3.13.2 (`python --version`) |
| Node.js | v22.14.0 (`node --version`) |
| Docker | 29.8.2 (`docker --version`) |
| Git | установлен (`/mingw64/bin/git`) |

Работоспособность Docker daemon и наличие LibreOffice/Ollama пока не проверялись.

### Изменённые файлы
- `docs/IMPLEMENTATION_STATUS.md` — создан.
- `.gitignore` — создан.

### Проверки
Тестов, линтеров и сборки на этом этапе нет — кода ещё нет.

### Ограничения / открытые вопросы
- Git-репозиторий инициализирован (`git init`), добавлен `.gitignore` (исключает `.env`, зависимости, кэши, загруженные файлы).

## Этап 01 — Аудит и архитектура (2026-10-15)

### Аудит репозитория
- `git status`: ветка `master`, рабочее дерево чистое, один коммит `99e82e5 Stage 00: project rules baseline`.
- Кода приложения, зависимостей (`pyproject.toml`, `package.json`) и тестов нет — сохранять нечего.
- `docker info`: daemon **не запущен** (`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`). `docker compose version`: v5.5.1.
- `soffice --version`: команда не найдена (LibreOffice на хосте нет — будет в контейнере `converter`).
- `curl http://localhost:11434/api/version`: ответа нет (локальный Ollama не запущен).

### Выполнено
- Спроектированы компоненты (frontend/nginx, backend, worker, converter, PostgreSQL, storage, LLM-провайдеры), модель данных, RBAC и сессии, загрузка и изолированная конвертация DOC, конвейер анализа с чанками и стабильными ID фрагментов, JSON-схема ответа, двухшаговая проверка доказательств (точное совпадение отдельно от смысловой поддержки), защита от prompt injection/SSRF/IDOR/XSS/CSRF/утечки секретов, облачное согласие, хранение/удаление, тестовая стратегия, backup/restore, production-развёртывание.
- Массовая реализация не начиналась.

### Изменённые файлы
- `docs/ARCHITECTURE.md` — создан.
- `docs/THREAT_MODEL.md` — создан.
- `docs/IMPLEMENTATION_PLAN.md` — создан.
- `docs/IMPLEMENTATION_STATUS.md` — дополнен.

### Проверки
Тестов, линтеров и сборки нет — этап документационный, кода нет.

### Ограничения / что нужно до следующих этапов
- Для интеграционных тестов с PostgreSQL, сборки образов и проверки DOC нужен запущенный Docker Desktop.
- Live-проверка OpenRouter требует API-ключ, Ollama — запущенный сервер с моделью; без них — только mocks с явной пометкой.

## Этап 02 — Каркас, БД, Docker (2026-10-15)

### Выполнено
- **Backend** (`backend/`): Python 3.13, FastAPI 0.143, SQLAlchemy 2.1 (async, psycopg 3), Alembic 1.20, pydantic-settings, structlog.
  - Типизированная конфигурация (`app/config.py`). Обязательные параметры проверяются при старте. В production процесс не стартует при `COOKIE_SECURE=false`, http-origin, пустых ключах, ключе-заглушке или `LOG_LEVEL=DEBUG`. Ошибка конфигурации выводится без значений переменных (exit 2).
  - Единый формат ошибок `{"error": {code, message, request_id, details}}`. Ответ на ошибку валидации не повторяет введённые значения. Ответ 500 не раскрывает детали исключения.
  - Middleware: request ID (принимается только безопасный формат), access-лог без query-строки, заголовки безопасности (CSP, nosniff, DENY, no-store; HSTS в production).
  - Структурированные JSON-логи с редактированием: секретные ключи → `[REDACTED]`, поля с содержимым (`text`, `quote`, `prompt`…) → `[CONTENT len=N]`, маскировка Bearer/`sk-…`/паролей в DSN, обрезка длинных строк.
  - `/api/health/live` и `/api/health/ready` (БД, миграции на head, запись в storage).
  - ORM-модели всех сущностей из ARCHITECTURE §5 (22 таблицы) и миграция `0001_initial`. Enum-поля хранятся как VARCHAR + CHECK. Документирован циклический FK documents ↔ document_versions.
  - Безопасность миграций: одна транзакция, advisory lock, `lock_timeout`, запрет downgrade в production без `ALLOW_DOWNGRADE=1`, тест-политика против разрушительных операций без пометки `# destructive-approved:`.
  - Хранилище `LocalStorage`: ключи генерирует сервер, путь проверяется (traversal невозможен), запись атомарная, лимит размера без остатков частичных файлов.
  - Очередь задач в PostgreSQL (enqueue с dedup, claim через `FOR UPDATE SKIP LOCKED`, heartbeat/lease, retry с backoff, `dead`, reaper) и worker (слоты параллелизма, graceful shutdown, heartbeat в БД и файл для healthcheck). Обработчиков задач пока нет — они появятся на этапах 04–07.
- **Frontend** (`frontend/`): React 19, TypeScript 6 (strict, `noUncheckedIndexedAccess`), Vite 8, Tailwind 4, React Router 7, TanStack Query 5, Zod 4; react-hook-form установлен для следующих этапов. HTTP-клиент с CSRF-заголовком для изменяющих запросов и разбором единого формата ошибок. Страница «Состояние системы» на реальном readiness API (без фиктивных данных), 404, правовая оговорка в подвале. oxlint (`react/no-danger`, `jsx-a11y`), Vitest + Testing Library + MSW.
- **Converter** (`converter/`): образ с LibreOffice (writer-nogui, без Java), запуск не от root, только `/health`. `/convert` отвечает 501 — конвертация будет на этапе 04.
- **Docker**: `compose.yaml` (db → migrate → backend/worker → frontend, converter, profile `ollama`), `compose.dev.yaml` (публикует БД только на 127.0.0.1). Сети: `data` и `convert` без выхода в интернет, у edge фиксированная подсеть для доверенного прокси. Все сервисы кроме db и ollama: read-only FS, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`. Healthchecks у всех сервисов. Volumes `pgdata`, `storage`, `ollama`. Backend-образ ставит зависимости только из `requirements.lock` с проверкой хешей (lock сгенерирован в linux-контейнере через `uv pip compile`).
- `.env.example`, `.gitattributes` (LF), `.dockerignore` для каждого образа, `deploy/backup.sh` и `deploy/restore.sh` (restore требует `--confirm`), `docs/OPERATIONS.md`, `README.md`.
- Документы приведены в соответствие: ARCHITECTURE (React 19, oxlint, compose.yaml в корне, CSP без `unsafe-inline`), THREAT_MODEL, IMPLEMENTATION_PLAN.

### Изменённые файлы
`.env.example`, `.gitattributes`, `.gitignore`, `README.md`, `compose.yaml`, `compose.dev.yaml`, `backend/**` (app, tests, alembic.ini, pyproject.toml, requirements.lock, Dockerfile, .dockerignore), `frontend/**` (src, nginx.conf, Dockerfile, package*.json, конфиги), `converter/**`, `deploy/backup.sh`, `deploy/restore.sh`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/IMPLEMENTATION_STATUS.md`.

### Проверки (реально запущены)
PostgreSQL для тестов: временный контейнер `postgres:17-alpine` (PostgreSQL 17.11) на `127.0.0.1:55432`, удалён после прогона.

| Команда | Результат |
|---|---|
| `alembic upgrade head` → `alembic check` → `alembic downgrade base` → `alembic upgrade head` (dev-БД) | успешно; `No new upgrade operations detected`; 23 таблицы (22 + `alembic_version`) |
| `ruff check app tests` | `All checks passed!` |
| `ruff format --check app tests` | `41 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 40 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `52 passed in 7.06s` |
| `pytest -q -rs` без `TEST_DATABASE_URL` | `40 passed, 12 skipped` (интеграционные пропущены с пояснением) |
| `npm run typecheck` | без ошибок |
| `npx oxlint --deny-warnings src` | exit 0 |
| `npx vitest run` | `Test Files 2 passed (2)`, `Tests 9 passed (9)` |
| `npm run build` | `✓ built` (JS 435 КБ / 134,5 КБ gzip) |
| `docker compose build` | собраны образы backend 349 МБ, converter 689 МБ, frontend 82,2 МБ |
| `docker compose up -d` | db, backend, worker, converter, frontend — `healthy`; migrate — `Exited (0)` |
| `curl http://127.0.0.1:8080/api/health/ready` | 200 `{"status":"ready","checks":{"database":"ok","migrations":"ok","storage":"ok"}}` |
| Заголовки | API: CSP `default-src 'none'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, `X-Request-ID`; SPA: CSP `default-src 'self'…`; deep link SPA → 200; `/api/nope` → 404 в едином формате |
| UID процессов | backend 10001, worker 10001, converter 10002, frontend 101 (nginx-unprivileged) |
| Изоляция | `docker compose port db 5432` — порт не опубликован; из converter нет DNS/интернета и нет доступа к `db`; запись в `/app` backend — `Read-only file system` |
| Логи | JSON; значения `POSTGRES_PASSWORD`, `SECRET_KEY`, `SECRETS_ENCRYPTION_KEYS` из `.env` в `docker compose logs` не найдены |
| Worker heartbeat | 1 запись в `worker_heartbeats`, свежее 1 минуты |
| Небезопасный production (`ENV=production`, http-origin, `COOKIE_SECURE=false`) | backend не стартует, exit 2, сообщение без значений переменных |
| `deploy/backup.sh` → изменение маркеров → `deploy/restore.sh` без `--confirm` → с `--confirm` | без `--confirm` — отказ (exit 2); после восстановления маркер в БД = исходному, файл в storage восстановлен (владелец `app`), readiness 200; тестовые маркеры удалены |

Найдено и исправлено в ходе проверки: ошибка `ValidationError` от pydantic печатала `input_value` (сырые значения окружения, потенциально секреты). Теперь выводится только имя поля и сообщение; добавлен тест `test_configuration_error_does_not_leak_values`.

### Ограничения
- Авторизации ещё нет (этап 03): API содержит только health-эндпоинты.
- Converter умеет только health; DOC-конвертация — этап 04.
- `ollama/ollama:latest` не закреплён по версии и в этом этапе не запускался.
- `requirements.lock` сгенерирован для linux/x86_64; на arm64-хостах его нужно перегенерировать.
- Нативный запуск `python -m app.server` на Windows не проверялся (psycopg async требует SelectorEventLoop; тесты и worker это учитывают). Рекомендуемый путь — Docker или WSL.
- Стек Docker Compose оставлен запущенным локально (`docker compose down` — остановить без удаления данных).

## Этап 03 — Авторизация, роли, bootstrap, аудит (2026-10-15)

### Выполнено
- **Пароли** (`app/security/passwords.py`): Argon2id (argon2-cffi, t=3, m=64 МиБ, p=4 — выше минимума OWASP), пересчёт хеша при смене параметров, политика (12–128 символов, не из списка распространённых, без e-mail/имени, не из одного-трёх символов), выравнивание времени ответа для несуществующего e-mail, генерация временных паролей (16 символов).
- **Сессии** (`app/security/sessions.py`): серверные сессии в `sessions`; в БД — только HMAC-SHA256(`SECRET_KEY`) токенов сессии и CSRF; cookie `cai_session` (HttpOnly, SameSite=Lax, Path=/api, Secure по `COOKIE_SECURE`) и `cai_csrf` (Path=/); скользящий idle (8 ч) и абсолютный (7 дней) TTL; новая сессия при входе и смене пароля; отзыв.
- **CSRF**: для изменяющих запросов — `X-CSRF-Token` именно этой сессии (сравнение в постоянном времени) + `Origin`/`Referer` из `ALLOWED_ORIGINS`; `POST /auth/login` — проверка `Origin`.
- **Rate limit** (`app/security/rate_limit.py`): скользящее окно в `login_attempts` (e-mail хранится как HMAC): 5 неудач на e-mail / 30 на IP за 15 мин → 429 + `Retry-After`; одинаково для существующих и несуществующих e-mail; сброс успешным входом или администратором; прогрессивная задержка ответа.
- **RBAC** (`app/security/policies.py`) и **scoped-доступ** (`app/security/scoped.py`): условие доступа в SQL для документов/анализов/экспортов (view/analyze/manage); чужой объект неотличим от несуществующего (404); Admin не читает чужие договоры; Superadmin — системный доступ по ID с аудитом `document.accessed_by_superadmin`, в общем списке — только свои и явно доступные.
- **Bootstrap** (`app/services/bootstrap.py`, `python -m app.cli bootstrap`, автоматически при старте backend): транзакция + advisory lock; идемпотентность (не меняет пароль/роль, не повышает существующего пользователя, не повторяется после `bootstrap_completed`); без параметров ничего не создаёт — readiness `accounts: bootstrap_required` (503) и предупреждение на странице входа; `must_change_password=true`; `BOOTSTRAP_ENABLED=false` отключает; напоминание в логе, если пароль остался в окружении.
- **API**: `/auth/login|logout|me|change-password|sessions|sessions/{id}/revoke|sessions/revoke-others`; `/users` (список с фильтрами и курсором, создание с одноразовым временным паролем, изменение имени/роли/блокировки, `reset-password`, `revoke-sessions`, `unlock`, `model-permissions` GET/PUT); `/admin/audit`; read-only `/documents`, `/documents/{id}`, `/analyses/{id}`, `/exports/{id}` (метаданные; загрузка/анализ/экспорт — этапы 04–07).
- **Правила ролей**: Admin не назначает Superadmin и не изменяет Superadmin; никто не меняет свою роль и не блокирует себя; последний активный Superadmin защищён; административные изменения сериализуются advisory lock с перечитыванием актора в транзакции. Найдено тестом и исправлено: встречное понижение двух Superadmin могло привести к deadlock и к проверке прав по устаревшей роли.
- **Аудит** (`app/services/audit.py`): запись в той же транзакции, что и действие; входы и неудачи (причина, IP, префикс HMAC e-mail — без введённого текста), выход, сессии, смена/сброс пароля, создание/роль/блокировка/разблокировка, доступ к моделям, доступ Superadmin к чужому документу. Коды действий для загрузок, анализов, удалений, экспортов и моделей определены — запись добавят этапы 04–07.
- **Readiness**: новая проверка `accounts` (есть ли активный Superadmin).
- **Frontend**: вход (react-hook-form + zod, безопасный параметр `next`), принудительная смена пароля (навигация скрыта), «Мой профиль» (сессии и их завершение), «Пользователи» (фильтры, пагинация, создание только с разрешёнными ролями, одноразовый показ временного пароля), карточка пользователя (роль, блокировка с подтверждением, сброс пароля, завершение сессий, снятие блокировки входа, доступ к моделям, защита Superadmin), «Журнал аудита»; глобальная обработка 401 → страница входа. Найдено тестом и исправлено: при истечении сессии страница могла упасть до редиректа — пользователь теперь передаётся через контекст.
- **Docker**: `BOOTSTRAP_*` передаются только сервису `backend`; `.env.example` дополнен.
- Документация: ARCHITECTURE §6.2–6.4, OPERATIONS §1 и новый §9 «Учётные записи и доступ».

### Изменённые файлы
`backend/app/{config.py, main.py, cli.py}`, `backend/app/api/{health,deps,auth,users,audit,resources}.py`, `backend/app/security/*`, `backend/app/services/*`, `backend/app/schemas/*`, `backend/pyproject.toml`, `backend/requirements.lock` (+argon2-cffi 25.1.0, argon2-cffi-bindings 26.1.0), `backend/tests/unit/{test_passwords,test_policies,test_auth_unit}.py`, `backend/tests/integration/{conftest,test_auth_api,test_bootstrap,test_users_api,test_access_matrix,test_database}.py`, `frontend/src/api/{auth,users,audit}.ts`, `frontend/src/app/{session.ts,guards.tsx,Layout.tsx,routes.tsx,queryClient.ts}`, `frontend/src/components/{ui,TemporaryPasswordNotice}.tsx`, `frontend/src/lib/format.ts`, `frontend/src/pages/{LoginPage,ChangePasswordPage,AccountPage}.tsx`, `frontend/src/pages/admin/*`, `frontend/src/i18n/ru.ts`, `frontend/src/test/auth.ts`, тесты `*.test.tsx`, `compose.yaml`, `.env.example`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/IMPLEMENTATION_STATUS.md`.

Миграций нет: нужные таблицы созданы на этапе 02; соответствие моделей схеме проверяет `test_schema_matches_models`.

### Проверки (реально запущены)
PostgreSQL для тестов: временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`, удалён после прогона.

| Команда | Результат |
|---|---|
| `ruff check app tests` | `All checks passed!` |
| `ruff format --check app tests` | `69 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 68 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `163 passed in 38.06s` |
| `pytest -q` без `TEST_DATABASE_URL` | `72 passed, 91 skipped` |
| `npm run typecheck` | без ошибок |
| `npx oxlint --deny-warnings src` | exit 0 |
| `npx vitest run` | `Test Files 4 passed (4)`, `Tests 38 passed (38)` |
| `npm run build` | `✓ built`; JS 517,8 КБ / 159,1 КБ gzip — предупреждение Vite о чанке > 500 КБ (разделение кода — на этапе 07) |
| `docker compose up -d --build` | db, backend, worker, converter, frontend — `healthy`; migrate — `Exited (0)` |
| Smoke через nginx (`127.0.0.1:8080`) | readiness 200, `accounts: ok`; лог `outcome: created`; вход с чужим Origin → 403; вход → 200, `must_change_password: true`; `/api/users` → 403 `password_change_required`; смена пароля без CSRF → 403, с CSRF → 200; затем `/api/users` → 200; cookie `cai_session` HttpOnly, Path=/api |
| Перезапуск backend | `outcome: skipped_superadmin_exists` + `bootstrap_password_still_configured`; в БД 1 пользователь; вход старым bootstrap-паролем → 401 |
| Аудит в БД | `user.bootstrap_created`, `auth.login_success`, `auth.password_changed` с IP |
| Пароли в логах | `docker compose logs` не содержит ни bootstrap-пароля, ни нового пароля (0 совпадений) |

Покрыто тестами: хеширование и политика; вход и неудачи (одинаковые ответы), заблокированный пользователь; rate limit по e-mail (в т.ч. несуществующий) и IP, сброс успешным входом и администратором; CSRF без токена, с токеном другой сессии, с чужим Origin; logout; idle/absolute TTL и скольжение; принудительная смена пароля; смена пароля отзывает остальные сессии и выдаёт новую; свои сессии (чужую отозвать нельзя — 404); rehash; bootstrap (пусто без параметров, создание, повтор без изменений, повтор после ручного понижения, конфликт e-mail, невалидные параметры, отключение, 5 параллельных запусков → 1 пользователь, старт приложения, коды выхода CLI); матрица ролей на всех эндпоинтах `/users` и `/admin/audit`; Admin против Superadmin; встречное понижение двух Superadmin; инвариант последнего Superadmin; блокировка отзывает сессии; сброс пароля; доступ к моделям; IDOR-матрица (7 ролей/ситуаций × документ/анализ/экспорт × свой/чужой/с доступом view/analyze/несуществующий/удаляемый), действия analyze/manage, понижение роли владельца, фильтрация списка, аудит доступа Superadmin, отсутствие названия договора в аудите; пароли и токены не попадают в логи и аудит.

### Ограничения
- Писем система не отправляет: временный пароль администратор передаёт пользователю вне системы.
- Записи аудита для загрузок, анализов, удалений, экспортов и управления моделями появятся вместе с этими функциями (этапы 04–07); коды действий уже определены.
- Роль БД без UPDATE/DELETE на `audit_log` — этап 08 (сейчас append-only обеспечивается кодом).
- В локальном Docker с пробросом порта IP клиента в аудите — адрес шлюза Docker (`172.30.10.1`); за внешним reverse proxy нужно передавать `X-Forwarded-For` (OPERATIONS §9).
- Локальный стек оставлен запущенным. В локальной БД создан Superadmin `root@example.org`; при smoke-тесте его пароль сменён (только для локального стенда; в репозиторий не записывается). В локальный `.env` (не коммитится) добавлены случайные `BOOTSTRAP_ADMIN_*`.

### Исправление после приёмки (2026-10-15)
- Симптом: вход из браузера отклонялся с `csrf_failed`. Причина: приложение открыто по `http://127.0.0.1:8080`, а в `ALLOWED_ORIGINS` был только `http://localhost:8080` (проверено: `Origin: http://127.0.0.1:8080` → 403, `http://localhost:8080` → 401 на неверных данных).
- `.env.example` (и локальный `.env`): `ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080`.
- Backend пишет в лог `csrf_rejected` с причиной (`origin_not_allowed` / `token_mismatch`) и полученным origin; добавлен тест `test_csrf_rejection_is_logged_with_reason`.
- Проверки: `ruff check` — `All checks passed!`; `mypy app tests` — без ошибок (68 файлов); `pytest -q` с PostgreSQL — `164 passed`; после `docker compose up -d --build backend worker` оба origin проходят проверку (401 на неверные данные), `http://evil.example` → 403 и запись `csrf_rejected` в логе.
