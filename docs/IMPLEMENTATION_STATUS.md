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

## Этап 04 — Загрузка и извлечение документов (2026-10-16)

### Выполнено
- **Приём файла** (`POST /api/documents`): тело запроса — сам файл (без multipart-парсера), имя — `X-File-Name`, повтор — `Idempotency-Key`. Лимит размера проверяется по `Content-Length` и потоково (`MAX_UPLOAD_MB`, 25 МБ); SHA-256 считается при записи; временный `.part`-файл удаляется при любой ошибке. Формат — по сигнатуре и структуре (`app/ingestion/formats.py`): PDF `%PDF-`; DOCX — ZIP с `word/document.xml` и типом основной части DOCX; DOC — OLE с потоком `WordDocument`. Расширение и `Content-Type` должны совпадать с форматом (`file_type_mismatch` / `mime_type_mismatch`, 415). ZIP-проверки до разбора: ≤ 2000 записей, ≤ 200 МБ в распакованном виде, коэффициент сжатия ≤ 100, без `..`, абсолютных путей, дубликатов и шифрования; DOCM и `vbaProject.bin` отклоняются; зашифрованные DOC/DOCX/PDF — `encrypted_document`. Имя файла санитизируется (без пути, управляющих и bidi-символов) и используется только для отображения. Лимит документов в очереди на пользователя (`MAX_PENDING_EXTRACTIONS_PER_USER`, 429). Оригинал — `storage/originals/<uuid>` (0600, вне web-root), выдаётся только через `GET .../versions/{vid}/file` с `attachment` и `nosniff`.
- **Извлечение** (задача `extract_document`, `app/ingestion/pipeline.py`): разбор в дочернем процессе `python -E -s -B -m app.ingestion.sandbox` с окружением только `PATH`/`LANG` (без секретов), `RLIMIT_AS/CPU/NOFILE/CORE`, таймаутом и убийством группы процессов; stderr не логируется.
  - PDF (PyMuPDF): блоки по страницам с bbox (`pdf_native`); зашифрованный, повреждённый или со слишком большим числом страниц → `failed` с кодом; без текста → `unsupported_no_text` (`no_text_layer`); страницы без текста — предупреждение; повторяющиеся колонтитулы помечаются `header_footer`, но остаются в тексте.
  - DOCX: zipfile + lxml с безопасным парсером (DOCTYPE/ENTITY отклоняются); абзацы и таблицы в порядке документа, content controls, вложенные таблицы, сноски; строка таблицы — фрагмент `table_row` (ячейки в `meta.cells`); автонумерация (decimal и др., `%1.%2.`, `startOverride`); исправления, надписи и колонтитулы — с предупреждениями.
  - Страницы DOCX: разметка Word (`docx_rendered_breaks`) — только если число `lastRenderedPageBreak` согласуется с `docProps/app.xml`; иначе рендер DOCX → PDF в converter и сопоставление текста (`libreoffice_render`, предупреждение о приблизительности); не найденный фрагмент остаётся без страницы.
  - DOC: converter DOC → DOCX (`converted_storage_key`), затем путь DOCX; предупреждение о конвертации.
  - `clause_ref` — только явный номер в тексте («4.2.», «Статья 5», «Раздел III») или вычисленная числовая автонумерация; иначе `null`. Заголовки — по стилям DOCX или явным текстовым признакам (источник указан в `meta`).
  - Сборка: канонический текст (`storage/text/<uuid>`, фрагменты через `\n\n`), стабильные ключи `F{seq:06d}`, смещения (code points), SHA-256 текста фрагмента; длинные абзацы делятся по предложениям (≤ 2000 символов); страницы — диапазоны символов. Фрагменты, страницы и статус записываются одной транзакцией; результат для удалённого за это время документа не сохраняется.
  - Недоступность converter — повтор задачи (5 попыток, backoff); на последней попытке DOC → `failed`, DOCX → `ready` без страниц. Неудачное извлечение повторяется через `POST .../retry-extraction`.
- **Converter** (`converter/server.py`): `POST /convert?from=doc&to=docx` и `?from=docx&to=pdf`; новый каталог и профиль LibreOffice на запрос (макросы запрещены: `MacroSecurityLevel=3`, `DisableMacrosExecution`; `BlockUntrustedRefererLinks`), один soffice одновременно, таймаут `LIBREOFFICE_TIMEOUT_SECONDS` с `killpg`, `RLIMIT_CPU/FSIZE/CORE`, проверка сигнатуры, в логе только метаданные.
- **API**: список (владелец, статус, флаги `can_manage/can_share/can_analyze`), карточка с версиями, `versions/{vid}`, `/pages`, `/fragments?page=&after_seq=`, `/file`, `DELETE` (статус `deleting` + задача `purge_document`: файлы, затем строки каскадом), выдача доступа (`GET|PUT /access`, `DELETE /access/{user_id}`, только владелец; Viewer — только `view`), `GET /documents/upload-limits`. Все объекты загружаются через scoped-запросы; версия проверяется через документ из URL. Аудит: `document.uploaded` (ID версии, формат, размер, SHA-256 — без имени файла), `document.deleted`, `document.file_downloaded`, `document.extraction_retried`, `document.access_granted/revoked`, доступ Superadmin.
- **Миграция** `0002_upload_idempotency` (неразрушающая): `documents.upload_idempotency_key` + частичный уникальный индекс (владелец, ключ).
- **Frontend** «Договоры»: drag-and-drop и выбор файлов, прогресс (XHR), проверка расширения и размера в браузере (только UX), ошибки с кодом запроса и повтором (с тем же `Idempotency-Key`), список со статусами и автообновлением. Карточка: метаданные, статус извлечения (ожидание с опросом, ошибка с причиной и повтором, «нет текстового слоя — OCR не поддерживается»), предупреждения, просмотр текста по страницам или целиком (ключ фрагмента, тип, пункт, страницы, цепочка заголовков, таблицы по ячейкам), скачивание оригинала, удаление с подтверждением, выдача доступа.
- **Docker**: параметры загрузки и извлечения в `compose.yaml`/`.env.example`; worker `mem_limit: 3g`, `pids_limit: 256`; переменные converter.
- **Документация**: ARCHITECTURE §8.6, OPERATIONS §10 (параметры, работа, диагностика), аудит в §9.

### Изменённые файлы
`backend/app/ingestion/*` (новый), `backend/app/api/documents.py` (новый; эндпоинты документов перенесены из `resources.py`), `backend/app/services/documents.py`, `backend/app/schemas/documents.py`, `backend/app/jobs/handlers/*` (новые), `backend/app/{config,storage,worker,main}.py`, `backend/app/jobs/registry.py`, `backend/app/models/documents.py`, `backend/app/security/{policies,scoped}.py`, `backend/app/services/audit.py`, `backend/app/db/migrations/versions/20261016_0002_document_upload_idempotency.py`, `backend/pyproject.toml`, `backend/requirements.lock` (+pymupdf 1.28.2, lxml 6.1.3, olefile 0.47, httpx 0.28.1), тесты `backend/tests/{docfactory.py, fixtures/, unit/test_ingestion_*.py, integration/test_documents_api.py}`, `converter/server.py`, `compose.yaml`, `.env.example`, `.gitignore`, `frontend/src/api/{client,documents}.ts`, `frontend/src/pages/documents/*`, `frontend/src/lib/{format,upload}.ts`, `frontend/src/app/{routes.tsx,Layout.tsx}`, `frontend/src/i18n/ru.ts`, `docs/{ARCHITECTURE,OPERATIONS,IMPLEMENTATION_STATUS}.md`.

### Проверки (реально запущены)
PostgreSQL для тестов: временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`, удалён после прогона.

| Команда | Результат |
|---|---|
| `ruff check app tests` | `All checks passed!` |
| `ruff format --check app tests` | `93 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 90 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `261 passed in 59.87s` |
| `pytest -q` без `TEST_DATABASE_URL` | `137 passed, 124 skipped` |
| `alembic upgrade head` → `alembic check` → `downgrade 0001_initial` → `upgrade head` | успешно; `No new upgrade operations detected.`; head `0002_upload_idempotency` |
| `npm run typecheck` | без ошибок |
| `npx oxlint --deny-warnings src` | exit 0 |
| `npx vitest run` | `Test Files 5 passed (5)`, `Tests 51 passed (51)` |
| `npm run build` | `✓ built`; JS 547,6 КБ / 167,0 КБ gzip (предупреждение Vite о чанке > 500 КБ — разделение кода на этапе 07) |
| `docker compose up -d --build` | db, backend, worker, converter, frontend — `healthy`; readiness 200 |
| Smoke через nginx `127.0.0.1:8080` (живой LibreOffice) | PDF → `ready`, 2 стр. `pdf_native`, пункты 1.1 / 1.2 / «Статья 2» / 2.1; DOCX → `ready`, `libreoffice_render`, таблица между абзацами, автонумерация 1/2; DOC → `ready` (DOC → DOCX → PDF в converter), предупреждения `converted_from_doc`, `pages_approximate`; скан → `unsupported_no_text` (`no_text_layer`); DOCX под именем `.pdf` → 415 `file_type_mismatch`; файл `MZ…` как `.docx` → 415 `unsupported_file_type`; оригинал DOC — 200, `application/msword`, `attachment`, `nosniff`, байты совпадают. Все 4 документа обработаны за 6 с |
| Многостраничный DOCX (один абзац ≈ 21 тыс. символов) | 8 стр. по рендеру, 11 частей абзаца со страницами 1–8, диапазоны страниц монотонны. **Найдено и исправлено:** до исправления все части получали стр. 1 — при сопоставлении с рендером не переносились переходы страниц внутри абзаца; добавлен регрессионный тест `test_rendered_pages_survive_paragraph_split` |
| Удаление в Docker | `DELETE` → 202; через 6 с документ 404, в логе `document_purged files: 2`, строки удалены |
| Лимиты дочернего процесса в Linux-контейнере worker | 1024 МБ — успешно; `RLIMIT_AS` 30 МБ — дочерний процесс падает, worker получает `extractor_crashed`; таймаут 0 с → `extraction_timeout` |
| Логи backend/worker/converter/frontend | текст договоров, имена файлов (в т.ч. percent-encoded) и пароль не найдены (0 совпадений); converter пишет только направление, размер, длительность |
| Изоляция | converter (uid 10002) не разрешает имя `db`; оригиналы `0600 app:app` |

Покрыто тестами: валидные PDF/DOCX/DOC; пустой файл; неизвестная сигнатура (HTML, EXE, PNG, текст); подмена расширения и MIME; zip-бомба; > 2000 записей в ZIP; DOCM и `vbaProject.bin`; path traversal и абсолютный путь в ZIP; не-Word ZIP и OLE; усечённый DOCX; XXE; превышение размера (по `Content-Length` и потоковое без него) без остатков в storage; пустой DOCX и скан → `unsupported_no_text`; зашифрованный и повреждённый PDF, превышение числа страниц (PDF и `app.xml`); порядок абзацев и таблиц, вложенные таблицы, content controls, исправления, сноски, надписи; автонумерация (многоуровневая, буквенная, `startOverride`); разметка Word и недоверие к ней при расхождении с `app.xml`; рендер LibreOffice (через mock converter) и сопоставление страниц; колонтитулы PDF; деление длинных абзацев; смещения и стабильность ключей; DOC через converter; недоступность converter (повтор → `failed`) и ручной повтор; идемпотентность загрузки; лимит очереди; роли (Viewer → 403), CSRF, 401; IDOR на документ, версию, страницы, фрагменты, файл, удаление и доступ для Analyst/Admin/Viewer и подмена версии другого документа; выдача и отзыв доступа (Viewer — только view; не-владелец и Superadmin не делятся); аудит доступа Superadmin; отсутствие текста и имени файла в логах и аудите; дочерний процесс (окружение без секретов, коды ошибок, таймаут); клиент converter (успех, 503/429/422/504, сетевая ошибка, пустой ответ). Frontend: список и статусы, нет загрузки у Viewer, загрузка с заголовками (имя, CSRF, Idempotency-Key), drag-and-drop, отказ по расширению и размеру без запроса, ошибка сервера с кодом запроса, текст по страницам, скан, ошибка извлечения и повтор, удаление, отсутствие кнопок без прав, выдача доступа, 404.

### Ограничения
- **Лицензия PyMuPDF — AGPL-3.0** (или коммерческая). Решение владельца продукта (2026-10-16): приложение — только для внутреннего использования, PyMuPDF остаётся. Условия, при которых решение нужно пересмотреть, и порядок замены на pypdfium2 — в `docs/LICENSES.md`.
- OCR не поддерживается: сканы получают `unsupported_no_text`.
- Страницы DOCX/DOC приблизительны (разметка Word или LibreOffice); фрагменты, которых нет в рендере (например, скрытый текст), остаются без страницы.
- Автонумерация: не поддерживаются `numStyleLink`, `isLgl`, `lvlRestart` — в таких списках `clause_ref` не заполняется; счётчики ведутся по `abstractNum`.
- Колонтитулы и надписи DOCX не извлекаются (с предупреждением). Порядок блоков PDF — порядок потока содержимого файла; в многоколоночной вёрстке он может отличаться от визуального.
- `RLIMIT_*` действуют только в Linux (Docker); при локальной разработке на Windows — только таймаут. При нехватке памяти уже на импорте библиотек код ошибки — `extractor_crashed`, а не `resource_limit_exceeded`.
- Загрузка новой версии существующего документа не реализована (промпт этапа этого не требует; модель данных её поддерживает).
- Лимиты загрузки задаются переменными окружения; редактирование через `system_settings` в UI — этапы 05/07.
- Пользователь для выдачи доступа ищется по точному e-mail; ответ 404 сообщает, что e-mail не зарегистрирован (внутри одной организации допустимо).
- Локальный стек оставлен запущенным; в локальной БД остались 4 синтетических документа smoke-теста (владелец `root@example.org`).

## Этап 05 — OpenRouter, Ollama и каталог моделей (2026-10-16)

### Выполнено
- **Единый `LLMProvider`** (`app/llm/base.py`): `generate_structured`, `check_connection`, `list_models`, `describe_model`; типы `StructuredRequest`/`StructuredResult`/`Usage`/`ProviderHealth`/`RemoteModelInfo` (repr не выводит содержимое сообщений). Нормализованные ошибки (`app/llm/errors.py`): `ProviderTimeout`, `ProviderRateLimited(retry_after)`, `ProviderUnavailable`, `ProviderAuthError`, `ProviderQuotaExceeded`, `ModelUnavailable`, `ContextTooLong`, `InvalidModelOutput`, `ProviderRequestRejected`, `ProviderNotConfigured`, `BlockedDestination` — с безопасными сообщениями без текста провайдера.
- **OpenRouter** (`app/llm/openrouter.py`, сверено с документацией 2026-10-16): `/chat/completions` с `response_format: json_schema (strict)` + `provider.require_parameters`, `json_object` для JSON-режима, инструкция со схемой для неподтверждённых моделей; без tools; ошибки в теле при HTTP 200 и `finish_reason: error`; `finish_reason: length` → `output_truncated`; usage/cost — только если вернул провайдер. `/key` — проверка ключа; `/models` — контекст, лимит вывода, цены (USD/токен → USD/1 млн; отрицательные = неизвестна), признак structured outputs.
- **Ollama** (`app/llm/ollama.py`): `/api/chat` (`stream:false`, `format` — схема или `"json"`, `options`), `/api/version`, `/api/tags`, `/api/show` (контекст из `model_info`); 404 → «модель не скачана, выполните ollama pull»; `/api/pull` не вызывается. Поддержаны контейнерная (`http://ollama:11434`, профиль compose `ollama`), хостовая и внешняя Ollama; предупреждение `loopback_in_container` для `localhost` в контейнере.
- **HTTP-клиент** (`app/llm/http.py`): раздельные таймауты (connect 10 с, read = `timeout_s` провайдера) и общий предел; лимит ответа 8 МБ (32 МБ для списка моделей); повторы только timeout/429/5xx/сеть — до 3, экспоненциальный backoff с jitter, `Retry-After` (секунды/HTTP-дата; > 60 с — без повтора); логируются только метаданные (`llm_http`, `llm_retry`).
- **SSRF-guard** (`app/llm/netguard.py`): http/https без userinfo/query/fragment; `http` — только с `allow_private_network`; разрешение DNS и проверка всех адресов на каждом запросе; подключение к проверенному IP с исходным `Host` и SNI (защита от DNS rebinding); metadata (AWS/GCP/Azure/Alibaba/Oracle, IPv4/IPv6, имена), link-local, multicast, unspecified, reserved, broadcast — всегда запрещены; loopback/private/CGNAT/ULA — только с флагом; IPv4-mapped/NAT64/6to4 разворачиваются; 3xx → `unexpected_redirect`; `trust_env=False`. Для локального провайдера (`is_cloud=false`) запрещены публичные адреса.
- **Секреты** (`app/security/secretbox.py`): MultiFernet из `SECRETS_ENCRYPTION_KEYS` (ключ — в env, отдельно от БД/бэкапов), в БД — шифротекст, отпечаток ключа и последние 4 символа; API отдаёт только `has_secret`/`secret_last4`; в ревизиях и аудите — `***`; смена адреса без нового ключа сбрасывает ключ; CLI `python -m app.cli rotate-secrets`.
- **Каталог в БД** (сервис `app/services/catalog.py`, API `app/api/catalog.py`): провайдеры (тип, адрес, облачность, включение, ключ, частная сеть, таймаут, параллелизм, заголовки без секретов, статус последней проверки) и модели (UUID — внутренний ID, название, внешний ID, включение, назначение `analysis|verifier|structure`, приоритет, одна модель по умолчанию, роли, контекст/лимит вывода, параметры генерации, режим structured output, цена с источником или `null`, сведения провайдера с датой, статус последней проверки). Оптимистическая блокировка (`version`, 409 `version_conflict`), история `config_revisions`, аудит. Проверка модели: наличие у провайдера + (по запросу) тестовая генерация; `json_schema` выставляется только после успешного вызова со схемой. Импорт моделей из каталога провайдера (выключенными). Удаление модели, использованной в анализах, и провайдера с моделями запрещено (только отключение). Ограничение параллельных генераций на провайдера в процессе (`max_concurrency`).
- **Доступность и облако**: `/api/models/available` и `resolve_model_for_use` (для этапа 06) — одно правило: роль Analyst+, модель и провайдер включены, назначение, персональное разрешение/запрет сильнее роли, облачные — только при включённом `cloud_models_enabled` (по умолчанию выключено). Текст предупреждения с версией (Superadmin), `ConsentRecord` на версию (`/api/models/{id}/cloud-consent`), `config_snapshot()` без секретов и полного URL — для `Analysis.config_snapshot`.
- **Mock-провайдер** (`app/llm/mock.py`) — только для тестов, в фабрике и БД не регистрируется.
- **Миграция** `0003_model_catalog` (неразрушающая): `model_configs.provider_metadata`, `model_configs.last_test_error_safe`, частичный уникальный индекс «одна модель по умолчанию».
- **Frontend**: раздел «Модели» (`/admin/models`, Admin/Superadmin): выключатель облака и текст предупреждения (редактирует только Superadmin), провайдеры (добавление/правка/удаление — Superadmin; ключ — поле password, после сохранения не показывается; проверка соединения; импорт моделей с ценами и контекстом), таблица моделей; страница модели (`/admin/models/:id`): параметры, роли, назначение, цена (не менять / неизвестна / вручную), проверка (с генерацией — с предупреждением о тарификации), история изменений, удаление. Компонент `ModelSelect` (показывает только модели из `/models/available`, для облачной — текст предупреждения и подтверждение) — используется в профиле («Модели, доступные мне для анализа»), на этапе 07 — в форме анализа. Capabilities `manage_models`, `manage_providers`, `manage_llm_settings` в `/auth/me`.
- **Документация**: ARCHITECTURE §12.1, OPERATIONS §11 (OpenRouter, варианты Ollama, частная сеть, облако, ротация ключа, диагностика) и аудит в §9.

### Изменённые файлы
`backend/app/llm/*` (новый), `backend/app/security/secretbox.py`, `backend/app/services/catalog.py`, `backend/app/schemas/catalog.py`, `backend/app/api/catalog.py` (новые), `backend/app/{main,cli}.py`, `backend/app/api/auth.py`, `backend/app/schemas/users.py`, `backend/app/security/policies.py`, `backend/app/services/audit.py`, `backend/app/models/catalog.py`, `backend/app/db/migrations/versions/20261016_0003_model_catalog.py`, тесты `backend/tests/{llm_helpers.py, unit/test_llm_netguard.py, unit/test_llm_providers.py, unit/test_secretbox.py, integration/test_catalog_api.py}`, `backend/tests/integration/conftest.py` (клиенты закрываются фикстурой, очистка `config_revisions`), `frontend/src/api/{catalog,auth}.ts`, `frontend/src/components/ModelSelect.tsx`, `frontend/src/lib/pricing.ts`, `frontend/src/pages/admin/{LlmPage,ModelPage}.tsx`, `frontend/src/pages/admin/llm.test.tsx`, `frontend/src/pages/AccountPage.tsx`, `frontend/src/app/{routes.tsx,Layout.tsx}`, `frontend/src/i18n/ru.ts`, `frontend/src/test/auth.ts`, `docs/{ARCHITECTURE,OPERATIONS,IMPLEMENTATION_STATUS}.md`, `CLAUDE.md`. Новых зависимостей нет (`httpx`, `cryptography` уже были).

### Проверки (реально запущены)
PostgreSQL для тестов: временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`, удалён после прогона.

| Команда | Результат |
|---|---|
| `ruff check app tests` | `All checks passed!` |
| `ruff format --check app tests` | `113 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 109 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `368 passed in 80.44s`; после исправления по live-проверке Ollama — `369 passed in 85.15s` |
| `pytest -q` без `TEST_DATABASE_URL` | `214 passed, 154 skipped in 11.99s` |
| `alembic upgrade head` → `alembic check` → `downgrade 0002_upload_idempotency` → `upgrade head` | успешно; `No new upgrade operations detected.`; head `0003_model_catalog` |
| `npm run typecheck` | без ошибок |
| `npx oxlint --deny-warnings src` | exit 0 |
| `npx vitest run` | `Test Files 6 passed (6)`, `Tests 61 passed (61)` |
| `npm run build` | `✓ built`; JS 588,3 КБ / 173,8 КБ gzip (предупреждение Vite о чанке > 500 КБ было и раньше — разделение кода на этапе 07) |
| `docker compose up -d --build` | db, backend, worker, converter, frontend — `healthy`; readiness 200; миграция `0003_model_catalog` применена |
| Smoke через nginx `127.0.0.1:8080` | облако по умолчанию выключено; `https://169.254.169.254/v1` → 422 `metadata_blocked`; `https://10.0.0.1/v1` → 422 `private_address_blocked`; OpenRouter с заведомо неверным ключом: **live** `GET /key` → `provider_auth_failed`; **live** `GET /models` → реальный каталог (через pinned IP + SNI с проверкой настоящего сертификата), `openai/gpt-4o-mini`: контекст 128000, вывод 16384, 0.15 / 0.6 USD за 1 млн; Ollama `http://ollama:11434` (контейнер не запущен) — локальный провайдер, проверка → `dns_failed`; `http://localhost:11434` → предупреждение `loopback_in_container`; модель Ollama → проверка `dns_failed`, `structured_output` остаётся `unknown`; в `/models/available` — локальная модель, облачных нет. Тестовые провайдеры и модель удалены |
| Live-проверка Ollama (2026-10-17, Ollama 0.40.4 на хосте Windows, CPU без GPU, `http://host.docker.internal:11434` + частная сеть) | без частной сети → 422 `https_required`; провайдер локальный (`is_cloud=false`); проверка соединения → `ok`, версия 0.40.4; список `deepseek-r1:1.5b`, `deepseek-r1:latest` (8.2B); импорт: контекст 131072 из `/api/show`, цена `null`. Проверка с генерацией: 8B — `json_schema` подтверждена; 1.5B — сначала `output_truncated` при лимите 200 токенов: модель расходует лимит на рассуждение (`thinking` ≈ 950 символов, `think:false` игнорирует). **Исправлено**: тестовая генерация использует лимит модели (≤ 2048), при обрезке не перебирает режимы и подсказывает увеличить лимит (регрессионный тест `test_model_test_truncated_output_suggests_larger_limit`); после исправления 1.5B — `json_schema` подтверждена за 39 с. Генерация по схеме рисков (синтетический текст из 2 фрагментов, лимит 4096) через `ProviderFactory`: 8B — 2 риска, верные ID фрагментов, цитаты дословные, 126 + 575 токенов, cost `None`; первая попытка — таймаут 300 с, повтор — 197 с (итого 498 с). 1.5B — 4 таймаута по 300 с; прямой запрос к Ollama: 346 с, `done_reason=length`, 4096 токенов рассуждения, ответ пустой — модель зацикливается в рассуждении (ограничение модели; при достаточном таймауте приложение вернёт `output_truncated`) |
| Live-проверка OpenRouter (2026-10-17, ключ владельца введён через UI) | проверка соединения → `ok`; `/models`: `openai/gpt-4o-mini` — контекст 128000, вывод 16384, 0.15 / 0.6 USD за 1 млн (`provider_api`); импорт; проверка с генерацией → `json_schema` подтверждена. Генерация по схеме рисков (синтетический текст из 2 фрагментов) через `ProviderFactory`: 3,0 с, `finish=stop`, 2 риска, верные ID фрагментов, цитаты дословные; usage 182 + 108 токенов, `cost` от провайдера 0.0000921 USD — совпадает с расчётом по ценам каталога. Строка ключа (`sk-or-`) не найдена в логах backend/worker/frontend, `audit_log`, `config_revisions`, шифротексте |
| Утечка ключа | значение ключа: 0 совпадений в логах backend/worker/frontend, `audit_log.details`, `config_revisions.diff`, ответах `/api/admin/providers` |

Покрыто тестами (mock-транспорт, SSRF-guard в цепочке): формат запросов OpenRouter/Ollama (схема, JSON-режим, инструкция, параметры, без tools, Bearer только при наличии ключа); usage и стоимость (только при наличии); timeout, 429 с `Retry-After` (повтор и отказ при слишком долгом ожидании), 5xx с ограниченным числом повторов, сетевая ошибка без деталей, 401/402/404/«not a valid model»/«context length», ошибка в теле при 200, `finish_reason` error/length, невалидный/не-объектный JSON без повторов, лимит размера ответа, отсутствие ключа OpenRouter, health и список моделей (пересчёт цен, неизвестная цена), Ollama: схема в `format`, 404 «не скачана» без `/api/pull`, timeout, `/api/version`, `/api/show`; SSRF: схемы (ftp, file, gopher), userinfo, query/fragment, некорректный порт, http без флага, metadata (IPv4, IPv6, имена, Alibaba), loopback (IPv4, IPv6, `localhost`, IPv4-mapped), частные, CGNAT, ULA, link-local, unspecified, multicast, broadcast, reserved, NAT64/6to4, pinned IP + Host + SNI, имя → частный адрес (без подключения), смешанный DNS-ответ, rebinding между запросами, числовая форма хоста, все 3xx, пустой DNS-ответ, локальный провайдер → публичный адрес; секреты: шифрование, ротация, неверный ключ, отсутствие ключа шифрования, ключ не попадает в логи/исключения/repr, ответы API, ревизии, аудит, `rotate-secrets`; API: матрица ролей (Superadmin/Admin/Analyst/Viewer) для провайдеров, моделей, настроек и истории; сброс ключа при смене адреса, запрет включения OpenRouter без ключа, конфликт версий, валидация адресов и заголовков (13 случаев), проверка соединения (успех, неверный ключ, локальный провайдер с публичным DNS), одна модель по умолчанию, запрет ручного `json_schema`, цена вручную/неизвестна, проверка модели с подтверждением schema и заполнением контекста/цены, недоступная модель, импорт (в т.ч. неизвестная цена и несуществующая модель), запрет удаления провайдера с моделями; доступность: роли, персональные разрешения и запреты, отключённая модель и провайдер, назначение, Viewer, облако выкл/вкл, согласие и его версия, `resolve_model_for_use` (404/409 коды), snapshot без секретов; capabilities. Frontend: список провайдеров без секрета и «цена неизвестна», ограничения Admin и Analyst, ключ — password и отправляется только при сохранении, ошибка SSRF с кодом запроса, проверка соединения, проверка модели с генерацией и недоступность ручного JSON Schema, сохранение с `version`, выбор модели (только серверный список, предупреждение и согласие для облачной, пустой список).

### Ограничения
- Live-генерация проверена на `openai/gpt-4o-mini` (OpenRouter) и deepseek-r1 (Ollama на CPU); другие модели подтверждаются кнопкой «Проверить с генерацией». Ошибки 429/402/таймауты OpenRouter вживую не воспроизводились — только mock.
- Распознавание «context length» и «модель не найдена» в ответах 400 OpenRouter — по тексту сообщения (стабильных кодов в документации нет); при изменении формулировок ошибка станет `provider_request_rejected`.
- Ограничение `max_concurrency` действует в пределах одного процесса (backend или worker), не глобально.
- Таймаут генерации (`timeout_s` провайдера, по умолчанию 120 с) повторяется как временная ошибка, и запрос начинается заново: для медленной локальной модели на CPU это удваивает ожидание (в live-проверке: таймаут 300 с, затем успех за 197 с). Для локальной Ollama на CPU задавайте `timeout_s` с запасом (300–900 с).
- Модели с рассуждением (deepseek-r1 и т.п.) расходуют лимит ответа и на рассуждение; `think:false` deepseek-r1:1.5b игнорирует. Лимит «Максимум токенов ответа» для них нужно задавать с запасом (этап 06 учтёт это при расчёте бюджета).
- Недоступный по DNS адрес проверяется долго (≈ 25 с: таймаут DNS 5 с × 4 попытки), так как `dns_failed` считается временной ошибкой.
- Цена при импорте берётся из `pricing.prompt/completion` OpenRouter (без цен за запрос, изображения, кэш); оценка стоимости анализа — этапы 06/07, только при известной цене.
- Каталог не кешируется (каждый запрос читает БД) — изменения применяются сразу; кеш с TTL из ARCHITECTURE §12 не понадобился.
- Выбора модели в форме анализа ещё нет (этап 07); `ModelSelect` сейчас показан в профиле.
- Предупреждение Vite о размере бандла (588 КБ) — разделение кода запланировано на этап 07.
- Локальный стек оставлен запущенным; тестовые провайдеры smoke-теста удалены (аудит и история изменений append-only); для live-проверки оставлены провайдер «Host Ollama» (`http://host.docker.internal:11434`, частная сеть, локальный) и модель `deepseek-r1:latest`, провайдер «OpenRouter» (ключ владельца) и модель `openai/gpt-4o-mini` — выключены, `json_schema` подтверждена; облачные модели выключены.

## Этап 06 — Анализ договора и доказательства (2026-10-17)

### Выполнено
- **Запуск** (`POST /api/analyses`, `app/services/analyses.py`): роль Analyst+ (403), доступ `analyze` к документу и версия `ready` через scoped-запросы (404/409), модель и необязательный верификатор — `resolve_model_for_use`, облако — выключатель + подтверждение в запросе + `ConsentRecord` на текущую версию предупреждения (409 `cloud_consent_required` / `cloud_notice_not_accepted`), оценка токенов ≤ `ANALYSIS_MAX_TOKENS` (422), активных анализов ≤ `ANALYSIS_MAX_ACTIVE_PER_USER` (429, под advisory lock), `Idempotency-Key` (повтор → тот же анализ, другой запрос → 409). Одной транзакцией: `Analysis` со snapshot (модель, провайдер, верификатор, версии конвейера/схемы/промптов/чанкера/нормализатора/детектора, чанкинг, лимиты, оценка), `AnalysisChunk`, задача `run_analysis`, аудит `analysis.started` (без текста). `POST /api/analyses/estimate` — оценка токенов и стоимости (только при известной цене) без запуска.
- **Конвейер** (`app/analysis/`, задача `run_analysis` в worker): детектор prompt injection → структура (разделы детерминированно; вид, предмет и стороны — моделью, имя стороны принимается только если найдено в тексте) → анализ чанков с перекрытием и стабильными ключами → межраздельный проход (противоречия и отсутствующие условия по заголовкам, ключевым фрагментам и карточкам рисков) → дедупликация → шаг A (точное/нормализованное совпадение, фрагмент существует и был в запросе модели, страницы и пункт — с сервера) → один повторный запрос точной цитаты → шаг B (верификатор в отдельном контексте, правила: цитата из заголовка ≤ `partially_supports`, у противоречия — два фрагмента) → итоговые статусы `verified_match` / `needs_review` / `rejected` → покрытие и сводка.
- **Риск**: ID (`R-001`…), title, category (18 категорий), kind (`clause_risk` / `contradiction` / `missing_clause`), severity, description, reason, consequences, affected_party, recommendation, suggested_revision (проект), model_confidence, несколько доказательств, validation_status + причины. Отсутствующие условия — отдельный тип без цитаты, с областью поиска (модель + проверенные разделы + ограничения), всегда `needs_review`. Отметки: ссылки на нормы права («требует проверки юристом»), ненейтральная формулировка при неизвестной стороне, объединение дубликатов.
- **Отчёт** (`GET /api/analyses/{id}`, `/risks`): сторона (код, имя, наименование из договора), модель/верификатор/snapshot, категории, глубина, счётчики по статусам и уровням, формулировка без «договор безопасен» (при отсутствии подтверждённых — «модель не выявила рисков с подтверждёнными цитатами… это не означает, что договор безопасен»), предупреждения, найденные фразы-инъекции (ключи фрагментов), ограничения, правовая оговорка; покрытие — фрагменты, чанки (неудачные/пропущенные), разделы (проанализирован / частично / нет), межраздельный проход, верификация, источник страниц. Отклонённые риски скрыты по умолчанию (`include_rejected=true`).
- **Защита от prompt injection**: текст только в `<fragment>` с экранированием `<`, `>`, `&`, `"` в атрибутах; системный промпт — текст данные; без инструментов, секретов, настроек; ответ — Pydantic `extra="forbid"`; страницы, пункты, статусы, права вычисляет сервер; эвристический детектор — предупреждение в отчёте.
- **Отмена / повтор / восстановление**: `POST /cancel` (в очереди — сразу, во время работы — прерывание текущего HTTP-вызова модели), `POST /retry` (для облачного — новое подтверждение; готовые чанки не пересчитываются), результаты этапов сохраняются сразу, fencing по `progress.run_id`, временные ошибки → повтор задачи (до 4 попыток), неустранимые → `failed` с безопасным кодом и сообщением, анализы без активной задачи закрываются (`job_lost`); аварийное отключение облака и отключение модели/провайдера останавливают уже идущий анализ; Ollama получает `num_ctx` по размеру запроса.
- **Миграция** `0004_analysis_pipeline` (неразрушающая): `analyses.stage_results`, `risk_evidence.attempt`, индекс `(created_by_id, status)`.
- **Настройки**: `ANALYSIS_MAX_TOKENS`, `ANALYSIS_MAX_ACTIVE_PER_USER`, `ANALYSIS_DEFAULT_MAX_OUTPUT_TOKENS`, `ANALYSIS_QUOTE_RETRY_LIMIT`, `ANALYSIS_CANCEL_POLL_SECONDS` (compose, `.env.example`).
- **Документация**: ARCHITECTURE §9.4 (уточнения реализации, в т.ч. интерпретация таблицы §10.3), OPERATIONS §12 (параметры, запуск через API, статусы, восстановление, диагностика), §11 — новое поведение аварийного отключения облака.
- **Исправлено по ходу**: тест этапа 04 `test_document_text_is_not_logged` проверял пустой вывод (`capsys` не перехватывает structlog) — переведён на `caplog`, теперь проверка реальная; ключ лога со словом `token` маскируется фильтром как секрет — переименован.

### Изменённые файлы
`backend/app/analysis/*` (новый пакет), `backend/app/services/analyses.py`, `backend/app/schemas/analyses.py`, `backend/app/api/analyses.py`, `backend/app/jobs/handlers/analyses.py` (новые), `backend/app/api/resources.py` (чтение анализа перенесено в `analyses.py`), `backend/app/{config,main,worker}.py`, `backend/app/jobs/{registry.py,handlers/__init__.py}`, `backend/app/models/{analyses,enums}.py`, `backend/app/services/audit.py` (`analysis.retried`), `backend/app/db/migrations/versions/20261017_0004_analysis_pipeline.py`, тесты `backend/tests/analysis_helpers.py`, `backend/tests/unit/test_analysis_units.py`, `backend/tests/integration/test_analysis_{api,evidence,pipeline}.py`, `backend/tests/integration/test_documents_api.py`, `compose.yaml`, `.env.example`, `docs/{ARCHITECTURE,OPERATIONS,IMPLEMENTATION_STATUS}.md`, `CLAUDE.md`. Новых зависимостей нет. Frontend не менялся (форма анализа и отчёт — этап 07).

### Проверки (реально запущены)
PostgreSQL для тестов: временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`.

| Команда | Результат |
|---|---|
| `ruff check app tests` | `All checks passed!` |
| `ruff format --check app tests` | `135 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 130 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `492 passed in 108.09s` |
| `pytest -q` без `TEST_DATABASE_URL` | `290 passed, 202 skipped in 10.09s` |
| `alembic upgrade head` → `alembic check` → `downgrade 0003_model_catalog` → `upgrade head` (чистая БД) | успешно; `No new upgrade operations detected.`; head `0004_analysis_pipeline` |
| `npm run typecheck`, `npx oxlint --deny-warnings src` | без ошибок |
| `npx vitest run` | `Test Files 6 passed (6)`, `Tests 61 passed (61)` |
| `npm run build` | `✓ built` (предупреждение о чанке > 500 КБ — как раньше, этап 07) |
| `docker compose up -d --build` | все сервисы `healthy`, readiness 200, миграция 0004 применена |
| **Live OpenRouter** (`openai/gpt-4o-mini`, синтетический PDF-договор из 14 фрагментов на 2 стр. с противоречием сроков оплаты, односторонним изменением цены, ограничением ответственности и строкой-инъекцией; глубина standard, все категории) | 1-й прогон: `completed`, 7 вызовов, 35 с, 9428 + 1652 токенов, cost от провайдера 0,0024 USD; strict JSON Schema принята; injection в F000010 обнаружен, модель его не выполнила (сводка не «безопасен»); страницы/пункты с сервера (2.2 → стр. 1, 3.2 → стр. 2). **Найдено и исправлено**: (1) в PDF заголовки «1. Предмет договора» — абзацы с пунктом → один раздел на весь договор: разделы теперь и по коротким пунктам верхнего уровня; (2) верификатор отвергал одну из цитат противоречия → уточнён промпт (`verifier-2`); (3) одно противоречие из чанка (`payment`) и межраздельного прохода (`contradiction`) не объединялось → противоречия сравниваются без категории; (4) при слиянии оставались вложенные цитаты одного фрагмента → остаётся более полная. Регрессионные unit-тесты добавлены. 2-й прогон: `completed`, 6 вызовов, 26 с, 0,0021 USD; 5 разделов; противоречие объединено (`merged_from: 2`) и `verified_match`; 3 `verified_match`, 1 `needs_review` (отсутствует порядок расторжения, с областью поиска). Итого затрачено ≈ 0,0045 USD |
| **Live Ollama** (deepseek-r1 8B на CPU хоста, глубина quick, 4 категории; таймаут провайдера временно 900 с) | `num_ctx` от сервера применён (Ollama: `context_length 12288`); вызов анализа чанка не завершился за 900 с трижды (машина параллельно выполняла тесты) — анализ ожидаемо повторял запрос; **отмена live**: `POST /cancel` во время 4-го запроса → HTTP-вызов прерван, статус `cancelled` через 2,7 с. Полный анализ на этой модели на CPU не дождались (часы) |
| Утечки | в логах backend/worker/frontend за 3 ч и в `audit_log.details`: 0 совпадений с фразами договора и `sk-or-` |

После live-проверки локальная конфигурация возвращена: OpenRouter и модели выключены, облачные модели выключены, таймаут Ollama 300 с (изменения записаны в историю конфигурации и аудит). В локальной БД остался синтетический документ live-проверки и 3 анализа по нему.

Покрыто тестами (сценарная модель, реальный PostgreSQL, настоящий worker): дословная цитата, нормализованная (NBSP, перевод строки, кавычки, тире, мягкий перенос, zero-width, NFC, перенос по слогам), слова и регистр не нормализуются, цитата через границу фрагментов и страниц, отсутствующая цитата (повтор → отзыв → `rejected`, верификатору не отправляется), исправление цитаты повтором (прежняя — `superseded`), неверная страница от модели (страница с сервера), лишнее поле `page` (repair), выдуманный fragment ID, фрагмент вне запроса модели и фрагмент другого документа, слишком короткая цитата, нерелевантная цитата (`does_not_support` → `rejected`), частичная поддержка, цитата из заголовка, сбой верификатора, противоречие (два фрагмента → `verified_match`, один → `needs_review`), отсутствующее условие с областью поиска, межраздельный проход пропускается без нужных категорий, риски вне категорий отбрасываются, дубликаты (похожие названия, перекрытие чанков), длинный документ (400 фрагментов, > 10 чанков, перекрытие, стабильные ключи, все фрагменты покрыты), unknown/named party, ссылки на нормы, prompt injection (экранирование, детектор, ссылка на «внедрённый» фрагмент отклоняется), невалидный JSON (repair один раз → чанк `failed` → `failed`/`completed_with_warnings`), обрезанный ответ без repair, исчерпание лимита токенов, временная ошибка провайдера (повтор задачи без пересчёта структуры), временная ошибка на последней попытке, неустранимая ошибка + `retry` без пересчёта готовых чанков, отмена в очереди и во время вызова модели (+ повтор после отмены), восстановление после падения worker'а и истечения lease, старый исполнитель не пишет после перехвата, «потерянный» анализ, отключение облака и модели во время/до выполнения, идемпотентность задачи и `Idempotency-Key`, роли и IDOR (Viewer, чужой, Admin, доступ view/analyze, Superadmin с аудитом), версия не готова / чужая, правила выбора модели и верификатора, облако (выключатель, согласие, предупреждение, повтор), валидация запроса и CSRF, лимит активных, бюджет и оценка стоимости, малый контекст, список и курсор, удалённый документ, отсутствие текста договора в логах; unit — нормализация, чанкинг, схемы и strict-совместимость JSON Schema, таблица итоговых статусов, дедупликация, детектор, промпты, разделы, сводка.

### Ограничения
- **UI анализа ещё нет** (форма запуска, прогресс, отчёт — этап 07 по плану); сейчас анализ запускается через API (OPERATIONS §12).
- Live: полный конвейер проверен только на `openai/gpt-4o-mini`. На deepseek-r1 8B (Ollama, CPU) один вызов анализа не уложился в 900 с — для локальных моделей на CPU нужны GPU, меньшая модель или таймаут больше; проверены только отмена и `num_ctx`.
- Смысловая проверка — оценка модели-верификатора (по умолчанию той же модели), не гарантия; отсутствие условия сервер доказать не может (всегда `needs_review`). Нормы права не проверяются — только отмечаются.
- Токены оцениваются по длине текста (3 символа/токен); фактический расход и стоимость — только если их вернул провайдер (Ollama стоимость не сообщает). Оценка при запуске — верхняя граница (live: 64 тыс. по оценке против 11 тыс. фактически).
- Детектор prompt injection эвристический (русские/английские фразы); основная защита — архитектурная.
- Повтор доступен только для `failed`/`cancelled`; часть с ошибкой в `completed_with_warnings` пересчитать можно только новым анализом. Повтор после этапа проверки доказательств не пересчитывает чанки.
- Межраздельный проход видит заголовки и ключевые фрагменты (по словарю терминов), а не весь текст — указано в области поиска отсутствующих условий.
- Ограничение `max_concurrency` провайдера по-прежнему действует в пределах процесса.

## Этап 07 — UI, результаты и отчёты (2026-10-17)

### Выполнено
- **Разделы UI** (на реальных API, без фиктивных данных): «Обзор» (выполняющиеся и последние анализы, последние договоры), «Договоры» (были; карточка дополнена анализами договора, кнопкой «Запустить анализ» и переходом к фрагменту из карточки риска с подсветкой и фокусом), «Анализы» (список с фильтром по статусу и автообновлением), «Новый анализ», страница анализа (отчёт), администрирование — пользователи, модели/провайдеры (секрет — только «задан …4 символа»), **«Настройки»** (облачный выключатель, версия предупреждения, системные параметры только для чтения — новый `GET /api/admin/system-settings`, Admin+), журнал аудита (добавлены подписи `analysis.*`, `export.*`); «Состояние системы» перенесено на `/status`.
- **Новый анализ**: документ/версия (только с текстом и правом анализа), сторона (A/B/название/не указана, с пояснениями), категории (все 18, «выбрать все/снять»), глубина, модель с облачным предупреждением и согласием, «Проверить модель и объём» (`/analyses/estimate`: части, токены, лимит, стоимость или «неизвестна», превышение лимита блокирует запуск), проверка полей до отправки, запуск с `Idempotency-Key` (один на форму), ошибки сервера с кодом запроса.
- **Прогресс и управление**: polling 2,5 с, пока анализ активен (этап, `<progress>` по частям, «временная ошибка — продолжится автоматически»), отмена с подтверждением, повтор (для облачного — повторное согласие), причина ошибки (безопасное сообщение). SSE не реализован (polling — основной путь по ARCHITECTURE §7).
- **Отчёт**: итог без «договор безопасен», счётчики по статусам проверки, сторона и наименование, модель/провайдер/верификатор/snapshot, категории, глубина, расход (стоимость — только от провайдера), предупреждения, найденные фразы-инъекции, ограничения, оговорка; охват (фрагменты, разделы, непроанализированные части, межраздельный проход, приблизительные страницы); **фильтры** по уровню, категории, затрагиваемой стороне, статусу проверки, типу, «показывать отклонённые».
- **Карточка риска**: три состояния различимы цветом, значком и текстом (✓ «Подтверждён» / ! «Требует проверки» с причинами / ✕ «Не подтверждён — не является установленным фактом»); у каждой цитаты отдельно — совпадение текста и смысловая связь; страница (+«приблизительно»), пункт, фрагмент со ссылкой в текст; длинные цитаты сворачиваются; заменённые повтором доказательства — под спойлером; рекомендация, проект редакции («для обсуждения, не готовый юридический текст»), уверенность («оценка модели, не вероятность»), «требует проверки юристом» для норм права; **отсутствующее условие** — область поиска, проверенные разделы, ограничения, без цитаты.
- **Экспорт** (backend `app/exports/`, `app/services/exports.py`, задача `build_export`, API в `app/api/resources.py`): полный отчёт PDF (reportlab, встроенный DejaVu Sans) и DOCX (python-docx), список рисков CSV (UTF-8 BOM, `;`) и XLSX (openpyxl, лист «Отчёт»). Содержимое: название документа, версия, даты, сторона, модель/snapshot (только перечисленные поля), категории, итог, счётчики, охват, риски с цитатами, страницами, пунктами, рекомендациями, отклонённые отдельно, ограничения, оговорка. Права — доступ `view` к документу (Viewer тоже), чужое → 404, только завершённый анализ (409), не готов (409), истёк (410), очередь (429); `attachment`, `nosniff`, `no-store`, имя без названия договора; аудит `export.created/downloaded`; срок хранения `EXPORT_TTL_HOURS`, файлы удаляет worker.
- **Защита экспорта**: PDF — экранирование разметки reportlab (теги, ссылки, изображения из текста модели/договора выводятся как текст); DOCX — только текст; удаление недопустимых для XML управляющих символов; CSV/XLSX — префикс `'` для значений с `= + - @ \t \r` (и полноширинных), в XLSX все ячейки строковые.
- **Backend**: `document_title` в ответах анализов; миграция `0005_export_details` (неразрушающая: `exports.error_code`, `exports.size_bytes`); `EXPORT_TTL_HOURS`, `MAX_PENDING_EXPORTS_PER_USER`; зависимости reportlab, python-docx (перенесён из dev), openpyxl, types-openpyxl (dev), `requirements.lock` перегенерирован; шрифты DejaVu + лицензия.
- **Доступность и адаптивность**: подписи у всех полей, `fieldset/legend`, `aria-live` для статусов, видимый фокус, переход к содержимому, нативный `<progress>`, статусы не только цветом; сетки Tailwind, горизонтальная прокрутка таблиц. **Разделение кода**: ленивые страницы (`app/lazyPages.tsx`) — основной бандл 403,6 КБ (125,6 КБ gzip), предупреждения Vite о чанке > 500 КБ больше нет.
- **Документация**: ARCHITECTURE §16.1, §17.1; OPERATIONS §13 (UI, экспорт, настройки), §12 обновлён; LICENSES (reportlab, openpyxl, Pillow, шрифты DejaVu).

### Изменённые файлы
Backend: `app/exports/*` (новый: `content.py`, `render.py`, `safety.py`, `fonts/`), `app/services/exports.py`, `app/jobs/handlers/exports.py`, `app/api/settings.py` (новые), `app/api/resources.py` (экспорт), `app/api/analyses.py`, `app/services/analyses.py`, `app/schemas/analyses.py`, `app/{config,main,worker}.py`, `app/jobs/handlers/__init__.py`, `app/models/system.py`, `app/db/migrations/versions/20261017_0005_export_details.py`, `pyproject.toml`, `requirements.lock`, тесты `tests/integration/test_exports_api.py`, `tests/unit/test_export_safety.py`, `tests/analysis_helpers.py`. Frontend: `src/api/{analyses,settings}.ts` (новые), `src/api/{client,catalog}.ts`, `src/app/{routes.tsx,Layout.tsx,lazyPages.tsx}`, `src/pages/OverviewPage.tsx`, `src/pages/analyses/*` (новые: страницы, карточка риска, фильтры, экспорт, значки, тесты), `src/pages/admin/{SettingsPage,LlmPage}.tsx`, `src/pages/documents/{DocumentPage,TextViewer}.tsx`, `src/i18n/ru.ts`, `src/test/{fixtures,handlers}.ts`, `src/pages/{StatusPage,auth}.test.tsx`. Прочее: `compose.yaml`, `.env.example`, `.gitattributes` (`*.ttf binary`), `docs/{ARCHITECTURE,OPERATIONS,LICENSES,IMPLEMENTATION_STATUS}.md`, `CLAUDE.md`.

### Проверки (реально запущены)
| Команда | Результат |
|---|---|
| `ruff check app tests` / `ruff format --check app tests` | `All checks passed!` / `145 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 139 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `520 passed in 113.98s` |
| `pytest -q` без `TEST_DATABASE_URL` | `310 passed, 210 skipped in 11.35s` |
| `alembic upgrade head` → `check` → `downgrade 0004_analysis_pipeline` → `upgrade head` (чистая БД) | успешно; `No new upgrade operations detected.`; head `0005_export_details` |
| `npm run typecheck` / `npx oxlint --deny-warnings src` | без ошибок / exit 0 |
| `npx vitest run` | `Test Files 7 passed (7)`, `Tests 80 passed (80)` |
| `npm run build` | `✓ built`; основной чанк 403,59 КБ (125,57 КБ gzip), страницы — отдельные чанки (AnalysisPage 20 КБ, NewAnalysisPage 7 КБ…); предупреждения о размере нет |
| `docker compose up -d --build` | все сервисы `healthy`, readiness 200, миграция 0005 применена |
| Smoke через nginx `127.0.0.1:8080` (завершённый live-анализ этапа 06 на `gpt-4o-mini`, новых вызовов LLM нет) | PDF/DOCX/CSV/XLSX `ready` за 1,6–2,6 с; `attachment; filename="contract-analysis-d85e3682.pdf"`, `nosniff`, `no-store`; PDF 3 стр., видимый текст только DejaVu Sans/Bold (кириллица), содержит название документа, сторону, модель, «стр. 1», «п. 2.2», ограничения и оговорку, без ключей/адресов провайдера; DOCX 87 абзацев; CSV с BOM, 4 риска; XLSX листы «Риски», «Отчёт»; анонимное скачивание → 401; SPA-маршруты `/`, `/analyses`, `/analyses/{id}`, `/admin/settings` → 200, CSP `script-src 'self'; style-src 'self'` без `unsafe-inline` |

Покрыто тестами. Backend: PDF (содержимое, экранирование разметки — теги и ссылка остаются текстом, гиперссылок нет, нет секретов и адресов провайдера), DOCX (текст, удаление управляющих символов), CSV/XLSX (formula injection: `=HYPERLINK`, `+cmd`, `@SUM`, все ячейки строковые, BOM, заголовки), права (чужой → 404 на создание/список/статус/скачивание, Viewer с доступом view экспортирует и скачивает, неверный формат 422), аудит, незавершённый анализ 409, не готов 409, истёк 410 + удаление файла, очередь 429, системные параметры только Admin+ и без секретов; unit — `cell`, `pdf_markup`, `clean_text`. Frontend: запуск анализа (проверка полей, облачное предупреждение и согласие, оценка с ценой, тело запроса, `Idempotency-Key`, CSRF, переход к прогрессу), превышение лимита блокирует запуск, ошибка сервера с кодом запроса, Viewer не запускает, прогресс через polling → результаты, три статуса доказательств и их тексты, отсутствующее условие с областью поиска, отклонённые скрыты до переключателя, фильтры (уровень, сторона, категория, статус, сброс, пустой результат), длинная цитата, текст модели не интерпретируется как HTML, пустой результат без «безопасен», ошибка 404 с кодом запроса, повтор облачного анализа с согласием, Viewer без отмены/повтора, экспорт (создание → формируется → ссылка), экспорт до завершения недоступен, обзор, список с фильтром статуса и без запуска для Viewer, настройки (только чтение, аналитику недоступны), карточка договора (анализы, запуск, переход к фрагменту с фокусом; без права анализа — без кнопки). Прежние тесты входа, загрузки, выбора модели и админки (секрет не показывается) проходят.

### Ограничения
- SSE для прогресса не реализован — только polling (2,5–3 с).
- Визуальная проверка UI в браузере не проводилась (тесты jsdom + сборка + проверка SPA-маршрутов и CSP через nginx); проверка контраста — по палитре Tailwind (тёмный текст `*-900/950` на светлых фонах), автоматического аудита доступности (axe/Lighthouse) нет — этап 08.
- Выбор отдельной модели-верификатора есть в API, но не в форме (по умолчанию — та же модель).
- Список договоров в форме анализа — первая страница (50); договор вне её открывается через «Запустить анализ» в карточке договора.
- Системные параметры в «Настройках» только отображаются; меняются через окружение и перезапуск.
- Экспорт содержит цитаты договора; срок хранения файла ограничен (`EXPORT_TTL_HOURS`), бэкапы хранилища содержат файлы до своей ротации.
- Приложение по-прежнему не скрывает риски вручную (`is_hidden_by_user` не используется в UI).

## Этап 08 — Безопасность, тесты и готовность к эксплуатации (2026-10-17)

### Аудит и исправления
Аудит по THREAT_MODEL: маршруты API и RBAC/IDOR (документы, текст/фрагменты, файлы, анализы, риски, экспорты, админка), CSRF/CORS/заголовки/cookies, XSS, SQL, rate limits, загрузки и лимиты ресурсов, SSRF, секреты и текст договоров в логах, prompt injection и JSON, облачное согласие, удаление и бэкапы, health checks. Найдено и исправлено:

| Проблема | Исправление | Тест |
|---|---|---|
| Смена пароля не ограничивала неверный «текущий пароль» — с перехваченной сессией можно было перебирать пароль, обходя лимит входа | неудача считается неудачей входа по e-mail (тот же лимит и задержка, 429 `password_change_rate_limited`), аудит `auth.password_change_failed`, успешная смена сбрасывает счётчик | `test_change_password_wrong_current_is_rate_limited`, `test_successful_password_change_resets_failure_counter` |
| Трассировки исключений (`unhandled_exception`, `job_crashed`, исключения из stdlib-логгеров) содержали текст сообщений: `input_value` ошибок валидации (ответ модели, фрагменты договора), `DETAIL` PostgreSQL со значениями полей | `safe_exc_info_processor`: типы исключений и кадры стека без сообщений (`[message hidden]`), цепочки `cause/context` | `test_exception_messages_are_not_logged`; e2e `check-logs` |
| Журнал аудита был append-only только на уровне кода | миграция `0006_audit_log_append_only` (неразрушающая): триггер запрещает UPDATE/DELETE строк `audit_log`; политика миграций допускает `op.execute` с пометкой `# non-destructive-sql:` только при отсутствии DROP/DELETE/TRUNCATE/UPDATE…SET/ALTER TABLE в SQL | `test_audit_log_is_append_only`, `test_non_destructive_sql_marker_rejects_data_changes`, `test_database` (upgrade/downgrade) |
| `href` ссылки скачивания экспорта брался из ответа API без проверки | Zod: только `/api/exports/<id>/download` | `не превращает подменённую ссылку скачивания в href` |
| `X-Forwarded-For` принимался от всей подсети edge | только от адреса nginx (`FRONTEND_EDGE_IP`); подсеть и адрес параметризованы (`EDGE_SUBNET`, `FRONTEND_EDGE_IP`) — нужно и для второго стенда | проверено на основном стенде: IP клиента в сессиях и аудите — `172.30.10.1`, не адрес nginx |
| У `backend` и `frontend` не было лимитов памяти/процессов | `mem_limit`/`pids_limit` (1 ГБ/256, 256 МБ/64) | `docker compose config`, оба стенда работают |
| Бэкап: файлы с правами по умолчанию, неполный бэкап оставался при ошибке, не фиксировалась ревизия схемы; восстановление `pg_restore --clean` оставляло таблицы более новой версии | `umask 077`, удаление неполного каталога, `MANIFEST` (`alembic_revision`), проверка ревизии перед восстановлением (код 3), пересоздание схемы `public` + `--exit-on-error` | e2e `backup-restore` на пустом стенде |
| Readiness не показывала, работает ли worker | `checks.worker` (heartbeat ≤ 120 с), не влияет на готовность API; предупреждение на странице «Состояние» | `test_ready_with_migrated_database`, `предупреждает, что worker не отвечает…` |
| Нет способа восстановить доступ при утере пароля единственного Superadmin (bootstrap повторно не выполняется) | `python -m app.cli reset-superadmin-password --email …`: временный пароль (выводится один раз), обязательная смена, завершение сессий, снятие блокировки входа, аудит `user.password_reset` (`via: cli`) | `test_cli_emergency_superadmin_password_reset` |

Проверено без замечаний: scoped-доступ (`scoped.py`) на всех объектных маршрутах, аудит обращений Superadmin, CSRF (+ Origin) на всех изменяющих маршрутах и входе, отсутствие CORS, CSP/заголовки SPA и API, cookies, `hide_parameters=True`, ключи хранилища, лимиты загрузки/извлечения, SSRF-guard (живой вызов провайдера Ollama в частной сети в e2e), строгая схема ответов модели и детектор инъекций, облачное согласие, удаление документов.

### Новые проверки
- **`tests/integration/test_security_sweep.py`** — маршруты берутся из OpenAPI-схемы (48 маршрутов): без сессии → 401; все изменяющие без CSRF-токена → 403 `csrf_failed` (и с чужим Origin); `/api/admin/*`, `/api/users*` для Analyst и Viewer → 403; каждый маршрут с ID документа/версии/анализа/экспорта и `POST /api/analyses(/estimate)` для постороннего Analyst → 404 `*_not_found` без названия договора и без изменения данных (≥ 20 маршрутов); контроль — владелец достигает тех же объектов; отсутствие CORS-заголовков; append-only аудит; новый неклассифицированный маршрут ломает тест.
- **E2E на отдельном чистом стенде** (`e2e/run.sh`, Playwright 1.63.0 + axe-core 4.13.0, mock-сервер с API Ollama; в приложение тестовый провайдер не добавлялся) — OPERATIONS §15.
- **Аудит зависимостей**: `pip-audit`, `npm audit`, `pip-licenses`, лицензии npm production (LICENSES.md).

### Документация
`docs/SECURITY_AND_PRIVACY.md` (новый: данные и сроки хранения, передача третьим лицам, меры, результаты аудита, остаточные риски); `docs/OPERATIONS.md` — §3 (проверки, аудит зависимостей, e2e), §4–5 (миграции, бэкап/восстановление: MANIFEST, проверка ревизии, последствия восстановления), §6 (логи), §7 (доверенный прокси, лимиты контейнеров), §9 (лимит смены пароля), **§14** (health checks, лимиты и восстановление после сбоев, ротация секретов, обновления и откат, очистка документов, отключение облака, роль БД для production, смена URL Ollama, аварийное восстановление), **§15** (e2e-стенд); `README.md`; `docs/THREAT_MODEL.md` (статус мер 4.2, 4.6, 4.10); `docs/LICENSES.md` (dev-зависимости, итог проверки лицензий); `CLAUDE.md`.

### Изменённые файлы
Backend: `app/logging.py`, `app/services/{auth,audit,users}.py`, `app/api/health.py`, `app/cli.py`, `app/db/migrations/versions/20261017_0006_audit_log_append_only.py` (новый); тесты `tests/integration/test_security_sweep.py` (новый), `tests/integration/{test_auth_api,test_bootstrap,test_database}.py`, `tests/unit/{test_logging,test_migration_policy}.py`. Frontend: `src/api/analyses.ts`, `src/pages/StatusPage.tsx`, `src/i18n/ru.ts`, тесты `src/pages/StatusPage.test.tsx`, `src/pages/analyses/analyses.test.tsx`; e2e: `e2e/*.e2e.ts`, `e2e/helpers.ts`, `playwright.config.ts`, `tsconfig.e2e.json`, `tsconfig.json`, `package.json` (скрипты `e2e`, `lint`, `build`; dev: `@playwright/test`, `playwright-core` 1.63.0, `@axe-core/playwright` 4.13.0), `package-lock.json`, `.dockerignore`. Инфраструктура: `compose.yaml`, `.env.example`, `.gitignore`, `deploy/{backup,restore}.sh`, `e2e/` (новый: `run.sh`, `compose.e2e.yaml`, `mock-llm/`, `fixtures/`, `make_fixtures.py`). Документация: см. выше.

### Проверки (реально запущены)
PostgreSQL для тестов — временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`.

| Команда | Результат |
|---|---|
| `ruff check app tests` / `ruff format --check app tests` | `All checks passed!` / `147 files already formatted` |
| `mypy app tests` (strict) | `Success: no issues found in 140 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `534 passed in 144.30s` |
| `pytest -q` без `TEST_DATABASE_URL` | `312 passed, 222 skipped in 10.68s` |
| `ruff check/format --check --config backend/pyproject.toml e2e` | `All checks passed!` / форматирование применено |
| Миграции на чистой БД: `alembic upgrade head` → `alembic check` → `downgrade 0005_export_details` → `upgrade head` | `No new upgrade operations detected.`; head `0006_audit_log_append_only`; триггер `audit_log_append_only` существует |
| `npm run typecheck` (приложение + e2e) / `npm run lint` (src + e2e) | без ошибок / exit 0 |
| `npx vitest run` | `Test Files 7 passed (7)`, `Tests 82 passed (82)` |
| `npm run build` | `✓ built`; основной чанк 403,64 КБ (125,61 КБ gzip) |
| `pip-audit -r backend/requirements.lock --no-deps --disable-pip`; по dev-venv | `No known vulnerabilities found` (оба) |
| `npm audit` / `npm audit --omit=dev` | `found 0 vulnerabilities` |
| `pip-licenses` (63 пакета), лицензии npm production | копилефт — только PyMuPDF (AGPL), psycopg (LGPL), certifi и pathspec (MPL-2.0); npm production — все MIT |
| **`bash e2e/run.sh all`** (чистый стенд `cai-e2e`: сборка образов, миграции на пустой БД, bootstrap) | exit 0 (≈ 7 мин): `flow` 2 passed, `recovery` 2 passed, `check-logs` — «логи чистые (609 строк)», `backup-restore` (схема `0006_audit_log_append_only`) — 1 passed; стенд удалён |
| `docker compose up -d --build` (основной стенд) | все сервисы `healthy`; readiness 200, `checks`: database/migrations/accounts/storage/worker — `ok`; миграция 0006 применена, триггер есть; вход/выход через nginx, IP клиента определяется корректно |

E2E подробно: `flow` — заголовки безопасности и CSP, 401 без сессии, отказ входа с чужим Origin; вход Superadmin из bootstrap → обязательная смена пароля → провайдер Ollama (mock, частная сеть) — проверка соединения и тестовая генерация (`json_schema` подтверждён) → загрузка PDF через UI → извлечение (`ready`, 2 стр., SHA-256 совпадает) → новый анализ через форму (оценка объёма) → `completed`; в отчёте: «Одностороннее изменение цены» и «Высокая неустойка» — «Подтверждён» (цитаты `exact`/`normalized`, `supports`, стр. 1, п. 2.2; через API — цитаты являются подстроками текста фрагментов), «Не определён порядок приёмки» — «Требует проверки» с областью поиска и без цитат, риск с выдуманной цитатой — `rejected`, скрыт по умолчанию и виден как «Не подтверждён» после переключателя; предупреждение о prompt injection (строка-инъекция в договоре); экспорт PDF (`%PDF`) и XLSX (`PK`) через UI, имя `contract-analysis-*`; аудит содержит `document.uploaded`, `analysis.started`, `export.created/downloaded` и не содержит текста и имени файла договора; axe (WCAG 2.1 A/AA) — 0 серьёзных/критических нарушений на страницах вход, обзор, договоры, карточка договора, новый анализ, отчёт. `recovery` — отмена во время вызова «медленной» модели → `cancelled` → повтор → `completed` (≈ 34 с); `SIGKILL` worker'а на этапе анализа частей → `docker compose up -d worker` → задача подхвачена после истечения lease, анализ `completed` с подтверждёнными рисками (≈ 2,6 мин). `check-logs` — в логах всех контейнеров (≈ 600 строк) нет фраз договора, паролей bootstrap/сменённого, `SECRET_KEY`, пароля БД и ключа шифрования. `backup-restore` — бэкап (схема `0006`) → `down -v` → пустой стенд → `restore.sh` (контрольные суммы, ревизия, пересоздание схемы, storage, migrate) → вход со сменённым паролем (bootstrap-пароль отклонён), SHA-256 оригинала совпадает, анализ `completed`, подтверждённые риски на месте. Визуальная проверка UI — по скриншотам Playwright при отладке сценария.

### Ограничения
- Live-проверки с реальными LLM на этапе 08 не проводились (платные запросы не нужны): e2e использует mock-сервер с API Ollama; живые OpenRouter/Ollama проверены на этапах 05–06.
- Политика хранения документов добавлена после приёмки этапа (см. ниже); удалённые документы остаются в бэкапах до их ротации.
- Шифрование бэкапов и выгрузка вне сервера — дополнительный этап 08A.
- Приложение подключается к БД суперпользователем из образа `postgres` — он может отключить триггер аудита; отдельная роль БД описана как рекомендация (OPERATIONS §14.7), не внедрена и не проверена.
- Общего rate limit на API нет (есть лимиты входа и смены пароля, очередей загрузки/экспорта, активных анализов); для публикации в интернет — лимиты на reverse proxy.
- Lighthouse не запускался; доступность проверена axe-core на 6 страницах (только нарушения уровня serious/critical считаются ошибкой; moderate/minor не анализировались).
- E2E-скрипт проверен в Git Bash на Windows (Docker Desktop); на Linux не запускался.
- Восстановление после `SIGKILL` занимает до `JOB_LEASE_SECONDS` + 60 с (reaper).

### Дополнение: политика хранения документов (2026-10-17, по запросу владельца)
- **Настройка** `system_settings.document_retention` = `{"days": N | null}`, по умолчанию выключена. `GET /api/admin/retention` (Admin+; состояние, число документов к удалению, предпросмотр `?preview_days=N`), `PUT /api/admin/retention` (только Superadmin, `policies.can_manage_retention`): срок 1–3650 дней; включение и сокращение срока — только с `confirm: true` (иначе 409 `retention_confirmation_required` с числом документов), выключение и увеличение — без подтверждения; `config_revisions` + аудит `settings.updated`.
- **Удаление** (`app/services/retention.py`, housekeeping worker'а раз в минуту, до 100 документов за проход, `FOR UPDATE SKIP LOCKED`): документы без активности дольше срока — загружены раньше срока, нет версий и анализов новее срока, нет активного анализа. Путь тот же, что у ручного удаления (`mark_for_deletion` → `purge_document`); аудит `document.deleted` без автора с `reason: retention`, `retention_days`.
- **UI** «Настройки» → «Хранение документов»: состояние для Admin; для Superadmin — переключатель, срок, предупреждение с числом документов, которые будут удалены сразу, и обязательная отметка подтверждения (кнопка «Сохранить» до неё недоступна).
- Файлы: `backend/app/services/retention.py` (новый), `app/api/settings.py`, `app/services/documents.py` (`mark_for_deletion` без автора), `app/security/policies.py`, `app/worker.py`; тесты `tests/integration/test_retention.py` (новый); frontend `src/pages/admin/RetentionSection.tsx` (новый), `SettingsPage.tsx`, `src/api/settings.ts`, `src/i18n/ru.ts`, `src/test/fixtures.ts`, тесты `src/pages/admin/retention.test.tsx` (новый), `src/pages/analyses/analyses.test.tsx`; документация — ARCHITECTURE §17, OPERATIONS §14.5, SECURITY_AND_PRIVACY §1, §11, §13. Миграция не нужна (таблица `system_settings` уже есть).

| Команда | Результат |
|---|---|
| `ruff check` / `ruff format --check` / `mypy app tests` | `All checks passed!` / `149 files already formatted` / `Success: no issues found in 142 source files` |
| `TEST_DATABASE_URL=… pytest -q` | `539 passed in 125.23s` |
| `pytest -q` без БД | `312 passed, 227 skipped in 10.71s` |
| `npm run typecheck` / `npm run lint` | без ошибок / exit 0 |
| `npx vitest run` | `Test Files 8 passed (8)`, `Tests 86 passed (86)` |
| `npm run build` | `✓ built` |
| `bash e2e/run.sh all` (регрессия) | exit 0: `flow` 2 passed, `recovery` 2 passed, логи чистые (609 строк), `backup-restore` 1 passed |
| Основной стенд `docker compose up -d --build` | readiness 200, все проверки `ok`; `GET /api/admin/retention` — `retention_days: null`; `PUT` без `confirm` → 409, настройка не изменилась (на локальных данных политика не включалась) |

Покрыто тестами: выключено по умолчанию (старый документ не удаляется), Admin не меняет (403), Analyst не видит (403), предпросмотр, 409 без подтверждения с числом документов, недопустимый срок (0, 3651) → 422, включение, увеличение без подтверждения, сокращение — с подтверждением, выключение, аудит и история конфигурации; worker удаляет только неактивный документ (сохраняются: свежий, с новой версией, с недавним анализом, с идущим анализом), повторный проход ничего не трогает, аудит без автора с `reason: retention`, `purge_document` удаляет документ и анализы; после выключения удаления нет. UI: состояние для Admin без формы, включение с предпросмотром и обязательным подтверждением (тело запроса), недопустимый срок блокирует сохранение, выключение без подтверждения.

## Этап 08A — Автоматические зашифрованные бэкапы вне сервера (2026-10-17)

Решения владельца (2026-10-17): значения по умолчанию — как в таблице `IMPLEMENTATION_PLAN.md`; настройки — **в UI и в `.env`** (Superadmin переопределяет в «Настройках» расписание, цель, сроки хранения и режим ротации; пути, адреса и ключи хранилища, ключ шифрования и webhook — только в `.env` сервера); живого внешнего хранилища нет — S3 проверяется на локальном S3-совместимом сервере.

### Выполнено
- **Скрипты на хосте** (`deploy/`): `backup.sh` — ручной запуск, `--scheduled` (опрос планировщиком), `--check`, `--list`, `--fetch <имя>`, прежний режим `backup.sh <каталог>`; `pg_dump` + архив storage + `MANIFEST`/`SHA256SUMS` → tar → `age -r` → `<UTC>.tar.age` + `.sha256` (права 600); выгрузка: `s3` (`rclone copyto`, сверка MD5 объекта, при отсутствии MD5 — сверка скачанной копии) или `local` (копирование + SHA-256); ротация после успеха: локально `BACKUP_KEEP_LOCAL`, во внешнем хранилище по схеме daily/weekly/monthly (`script`) или ничего (`lifecycle`); самый новый бэкап не удаляется никогда, при ошибке выгрузки локальные копии не удаляются; блокировка (`flock` или каталог с PID — в Git Bash нет flock); журнал `BACKUP_DIR/backup.log`; уведомление webhook об ошибке (повторяющиеся ошибки опроса — не чаще раза в сутки); минимальный интервал `BACKUP_MIN_INTERVAL_MINUTES` для запусков по расписанию и из UI. `restore.sh` принимает `.tar.age --identity <ключ>` (проверка `.sha256`, расшифровка в контейнере). `backup-schedule.sh install|uninstall|status` — systemd timer или crontab (опрос раз в 5 минут; после изменения расписания переустановка не нужна). `lib/backup-common.sh` — чтение `.env` без исполнения (только `BACKUP_*`), GFS-ротация, блокировка, уведомления. Образ `contract-ai-analyzer/backup-tools:local` (Alpine 3.22: age 1.2.1, rclone 1.69.3, curl; не root) — на хост ничего не ставится.
- **Граница доверия**: вывод `app.cli backup-plan` — только строки `KEY=VALUE`; скрипт принимает ключи из белого списка, проверяет значения регулярными выражениями, не исполняет; все параметры повторно проверяются на хосте. Ключи S3 и URL webhook передаются контейнеру инструментов через окружение (имена переменных в аргументах), в контейнеры приложения не попадают. Docker socket контейнерам не выдаётся.
- **Backend**: `app/backup/cron.py` (cron 5 полей, диапазоны/шаги/списки, макросы, OR для дня месяца и недели, часовой пояс, «никогда не срабатывает» → ошибка), `app/services/backup.py` (эффективные настройки UI поверх `.env`, проверки и понятные сообщения, «пора ли» — по расписанию с одним догоняющим запуском или по запросу, отметка опроса планировщика, отчёты, «прерванные» запуски через 6 ч, «просрочен»), `app/api/backup.py` (`GET /api/admin/backup` Admin+, `PUT` и `POST /run` только Superadmin; изменение, вносящее новые проблемы, отклоняется 422), CLI `backup-plan` / `backup-report`, миграция `0007_backup_runs` (неразрушающая), аудит `backup.requested/completed/failed`, история конфигурации, `BACKUP_*` (несекретные) в окружении backend/worker/migrate.
- **UI** «Настройки» → «Резервное копирование»: проблемы настроек, предупреждения «планировщик не опрашивал» и «бэкап просрочен», последний успешный бэкап, следующий запуск, «Запустить сейчас»; параметры с пометкой источника (`из .env` / `задано в интерфейсе` + «вернуть значение из .env»), шаблоны расписания, цели (ненастроенные на сервере недоступны), ключ шифрования (начало и конец), режим ротации; журнал запусков. Admin — только просмотр.
- **Документация**: OPERATIONS §16 (устройство, параметры, первоначальная настройка, один запуск, ротация и защита от взлома, восстановление, ротация ключа, диагностика), §5 обновлён; `.env.example`; SECURITY_AND_PRIVACY; ARCHITECTURE §17; CLAUDE.md.

### Отклонения от плана
- **Расписание применяется без переустановки таймера**: план предполагал, что `backup-schedule.sh install` создаёт таймер по `BACKUP_SCHEDULE`. Так как расписание можно менять в UI (решение владельца), таймер опрашивает приложение раз в 5 минут, а время по cron вычисляет приложение; изменение в UI или `.env` действует без доступа к серверу.
- **S3 проверен на `rclone serve s3`, а не на MinIO**: образы MinIO больше не публикуются (`minio/minio` и `quay.io/minio/minio` — pull access denied / 401). Выгрузка в реальное облачное хранилище не проверялась.
- **Уведомление** — только webhook (e-mail — через внешний webhook-шлюз).

### Проверки (реально запущены)
PostgreSQL для тестов — временный контейнер `postgres:17-alpine` на `127.0.0.1:55432`.

| Команда | Результат |
|---|---|
| `ruff check` / `ruff format --check` / `mypy app tests` | `All checks passed!` / `156 files already formatted` / `Success: no issues found in 148 source files` |
| `TEST_DATABASE_URL=… pytest -q` | первый прогон: `570 passed`, 1 failed — `test_schema_matches_models` (в списке таблиц теста не было новой `backup_runs`; добавлена); повторный полный прогон: `571 passed in 131.32s` |
| `pytest -q` без БД | `336 passed, 235 skipped in 10.99s` |
| Миграции на чистой БД: `upgrade head` → `alembic check` → `downgrade 0006_audit_log_append_only` → `upgrade head` | `No new upgrade operations detected.`; head `0007_backup_runs` |
| `npm run typecheck` / `npm run lint` / `npx vitest run` / `npm run build` / `npm audit` | без ошибок / exit 0 / `Test Files 9 passed (9)`, `Tests 90 passed (90)` / `✓ built` / `found 0 vulnerabilities` |
| ShellCheck 0.10.0 (`-x -S warning`) по `deploy/backup.sh`, `restore.sh`, `backup-schedule.sh`, `lib/backup-common.sh`, `e2e/backup-offsite.sh`, `e2e/run.sh` | без замечаний (до исправления — переменная `keep` совпадала с массивом в `gfs_keep`, `ls \| grep`, неэкранированный `$(…)`) |
| **`bash e2e/run.sh all`** с нуля (новый стенд; `flow`, `recovery`, `check-logs`, `backup-restore`, `backup-offsite`, `restore`) | exit 0: flow 2 passed, recovery 2 passed, логи чистые (596 строк), restore после простого бэкапа 1 passed, **08A — 11/11**, restore после восстановления из S3 1 passed |
| Основной стенд `docker compose up -d --build` | readiness 200, все проверки `ok`; миграция 0007 применена; `GET /api/admin/backup` — цель `none`, `0 3 * * *`, проблем нет, следующий запуск 2026-10-18 03:00 UTC; планировщик на хосте разработки не устанавливался (Windows) — UI показывает предупреждение |

Проверки шага `e2e/backup-offsite.sh` (реальные скрипты на стенде `cai-e2e`, Git Bash на Windows + Docker Desktop):
1. `--check` с настройками по умолчанию.
2. Отказ с понятной ошибкой и без бэкапа: неверный ключ age, каталог-цель не существует или совпадает с `BACKUP_DIR`, нет секрета S3, нет ключа шифрования для выгрузки; UI отклоняет `keep_local=0`.
3. Шифрование: `.tar.age` с заголовком `age-encryption.org/v1`, расшифровка ключом администратора, `SHA256SUMS` и `MANIFEST` (ревизия `0007`) совпадают; чужим ключом не расшифровывается.
4. Цель `local` (выбрана в UI): копия и `.sha256` во внешнем каталоге, сверка SHA-256; ротация локальных копий до `keep_local=2` (из UI) и внешних по схеме 1/0/0 (удалены 2 «старые» копии и предыдущая); отчёт в UI: `success`, выгружен, проверен.
5. Сбой выгрузки (неверный секрет S3): код 1, локальные копии не удалены, webhook `{"error_code":"upload_failed"}` без секрета, в UI — `failed` / `upload_failed`.
6. Цель `s3` (`rclone serve s3`): копия выгружена, ротация локальных; режим `lifecycle` не удаляет внешние копии; `--list`. Проверка копии в хранилище: **`rclone serve s3` не сообщает MD5**, поэтому сработала запасная сверка скачанной копии (в журнале — «хранилище не сообщает MD5 — сверка скачанной копии»); сверка по MD5 самого хранилища на реальном S3 не проверялась.
7. Параллельный запуск: ручной — код 3, опрос планировщика — тихий пропуск (блокировка каталогом с PID; ветка `flock` — только на Linux, не запускалась).
8. Планировщик (`--scheduled`): без повода не запускает; запрос «Запустить сейчас» не выполняется раньше `BACKUP_MIN_INTERVAL_MINUTES`, затем выполняется (`trigger=requested`, запрос снят, отметка опроса планировщика есть); расписание из UI («каждую минуту») срабатывает (`trigger=scheduled`); выключенное расписание не срабатывает.
9. Полный цикл: бэкап → выгрузка в S3 → удаление всех локальных копий → `--fetch` (проверка SHA-256) → `restore.sh … --identity … --confirm` (расшифровка) → Playwright: вход со сменённым паролем, SHA-256 оригинала, анализ и подтверждённые риски на месте.
10. `backup-schedule.sh` в контейнерах Linux: crontab (Alpine) — установка, повторная установка без дублей, `status`, удаление; systemd (Debian trixie) — unit-файлы `OnCalendar=*:0/5`, `ExecStart=… --scheduled`, `systemd-analyze verify` без ошибок (кроме отсутствующего в контейнере `docker.service`), удаление.
11. Секрет S3 и приватный ключ age не найдены в `backup.log` и логах сервисов.

Backend-тесты: `tests/unit/test_backup_cron.py` (24: ежедневное, часовой пояс, шаги, диапазоны, списки, день недели 0/7, OR дня месяца и недели, макросы, 29 февраля, некорректные выражения, «31 февраля»), `tests/integration/test_backup_api.py` (8: значения из .env и только чтение для Admin, 403 для Admin/Analyst, переопределение и возврат к .env с аудитом и историей, запрет цели, не настроенной на сервере, и показ серверных параметров без ключей S3, проблемы .env блокируют план и выводятся `PROBLEM=`, логика «пора» и отчёты с аудитом, «Запустить сейчас», ошибки и «прерванные» запуски + «просрочен», проверка ввода `backup-report`). Frontend: `src/pages/admin/backup.test.tsx` (4).

### Ограничения
- Выгрузка в реальное облачное S3-хранилище не проверялась (нет доступа); S3-совместимость — на `rclone serve s3`. MinIO недоступен (образы не публикуются).
- Скрипты проверены в Git Bash на Windows с Docker Desktop; на Linux-хосте целиком не запускались (cron и systemd — только установка/проверка unit-файлов в контейнерах; живой systemd timer не запускался).
- Уведомление — webhook; e-mail напрямую не отправляется.
- Время в UI показывается в часовом поясе браузера, расписание — в `BACKUP_TIMEZONE`.
- Для `BACKUP_REMOTE_PRUNE=script` на S3 ключу нужны права на удаление (компрометация сервера позволяет стереть копии) — по умолчанию для S3 `lifecycle`; правила lifecycle настраиваются в хранилище вручную (OPERATIONS §16.5).

