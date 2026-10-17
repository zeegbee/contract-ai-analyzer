# Contract AI Analyzer — архитектура

Статус: проект архитектуры (этап 01). Реализация начинается с этапа 02. Документ описывает целевое состояние; что реально сделано и проверено — только в `IMPLEMENTATION_STATUS.md`.

Связанные документы: [THREAT_MODEL.md](THREAT_MODEL.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

---

## 1. Назначение и границы

Русскоязычное веб-приложение для предварительного анализа договоров (DOC, DOCX, PDF с текстовым слоем): извлечение текста со страницами и фрагментами, поиск юридических и коммерческих рисков с помощью LLM (OpenRouter или локальный Ollama), серверная проверка каждой цитаты, отчёты и экспорт, управление пользователями, моделями и настройками.

Не входит в обязательный объём: OCR сканированных PDF, редактирование договоров, электронная подпись, multi-tenant SaaS (одна организация на инсталляцию), мобильные приложения.

Система — инструмент поддержки анализа, не юридическая консультация. Это отражается в UI, отчётах и экспорте.

## 2. Ключевые решения

| # | Решение | Обоснование |
|---|---|---|
| D1 | Монорепозиторий: `backend/`, `frontend/`, `converter/`, `deploy/`, `docs/`; `compose.yaml` в корне | Один набор версий, один compose-файл |
| D2 | Backend: Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.x (async, драйвер psycopg 3), Alembic | Предпочтительный стек; Python 3.13 установлен на машине разработки и используется в образах, чтобы окружения совпадали |
| D3 | PostgreSQL 17 — единственное хранилище состояния, включая очередь задач | Без Redis/брокера: меньше компонентов; `SELECT … FOR UPDATE SKIP LOCKED` даёт надёжную очередь с транзакционными гарантиями |
| D4 | Отдельный процесс `worker` (тот же образ, что backend) выполняет извлечение, анализ, экспорт, очистку | Длительные операции не держат HTTP-запросы; worker перезапускается независимо |
| D5 | Отдельный контейнер `converter` с LibreOffice для DOC→DOCX и DOCX→PDF (для разметки страниц) | Изоляция недоверенного парсера: без интернета, без доступа к БД/хранилищу, лимиты CPU/RAM, read-only FS |
| D6 | Серверные сессии в БД + HttpOnly cookie; CSRF-токен (synchronizer) + проверка `Origin` | Возможность отзыва сессий; JWT в localStorage уязвим к XSS |
| D7 | Frontend и API на одном origin: nginx отдаёт SPA и проксирует `/api` | CORS не нужен; SameSite-cookie работают предсказуемо |
| D8 | Файлы на диске (volume) за абстракцией `StorageBackend` | Просто для Docker Compose; позже можно подключить S3-совместимое хранилище |
| D9 | Единый интерфейс `LLMProvider`, адаптеры OpenRouter и Ollama на `httpx` без SDK | Полный контроль над таймаутами, редиректами, SSRF-защитой и логированием |
| D10 | Секреты провайдеров хранятся в БД зашифрованными (Fernet/MultiFernet), ключ — только в env/secret manager | Каталог провайдеров меняется без пересборки и рестарта; ключ отделён от данных; поддержана ротация |
| D11 | Проверка доказательств в два независимых шага: точное совпадение (детерминированно) и смысловая поддержка (отдельный LLM-проход верификатора + правила) | Требование: совпадение текста само по себе не доказывает риск |
| D12 | ID всех сущностей — UUID; доступ к чужому объекту возвращает 404 | Нет перебора ID, нет утечки факта существования |

## 3. Компоненты и развёртывание

```
                    ┌──────────── Browser (SPA, ru) ────────────┐
                    └──────────────────┬─────────────────────────┘
                                       │ HTTPS (в prod — внешний reverse proxy: Caddy/Traefik/nginx)
                              ┌────────▼────────┐
                              │  frontend       │ nginx: static SPA + proxy /api → backend
                              └────────┬────────┘   security headers, CSP
                                       │ network: edge
                              ┌────────▼────────┐
                              │  backend (API)  │ FastAPI/uvicorn, без root
                              └───┬────────┬────┘
                    network: data │        │ network: egress (только backend/worker)
               ┌──────────────────┤        └──────────► OpenRouter (HTTPS), внешний Ollama
        ┌──────▼─────┐   ┌────────▼───────┐
        │ postgres   │◄──┤ worker         │ та же кодовая база, `python -m app.worker`
        │ (no ports) │   └───┬────────┬───┘
        └────────────┘       │        │ network: convert (internal: true, без выхода в интернет)
                     volume: │  ┌─────▼──────┐
                     storage │  │ converter  │ LibreOffice headless, read-only FS, tmpfs, mem/cpu limits
                             │  └────────────┘
                     (backend и worker монтируют storage; converter — нет)

        ollama (опционально, compose profile `ollama`) — в сети egress/llm, либо внешний хост
```

| Сервис | Образ | Порты наружу | Volumes | Сети |
|---|---|---|---|---|
| `frontend` | nginx (unprivileged) + собранный SPA | 8080 (локально); в prod — за reverse proxy | — | edge |
| `backend` | python:3.13-slim, пользователь `app` | нет (через frontend) | `storage` | edge, data, egress |
| `worker` | тот же образ, другой command | нет | `storage` | data, egress, convert |
| `converter` | debian-slim + libreoffice-writer-nogui, пользователь `conv` | нет | tmpfs | convert (`internal: true`) |
| `db` | postgres:17 | нет (в dev — опционально `127.0.0.1:5432`) | `pgdata` | data |
| `ollama` | ollama/ollama (profile) | нет | `ollama` | llm |

Миграции: отдельная одноразовая команда/сервис `migrate` (`alembic upgrade head`), запускается перед стартом backend/worker; backend при старте проверяет, что схема на `head`, и не стартует (readiness=false), если нет. Автоматический downgrade не выполняется.

Health/readiness:
- `GET /api/health/live` — процесс жив.
- `GET /api/health/ready` — БД доступна, миграции на `head`, storage доступен на запись. Не делает внешних запросов к LLM.
- worker пишет heartbeat в таблицу `worker_heartbeat`; healthcheck контейнера проверяет его свежесть.

## 4. Структура репозитория (целевая)

```
backend/
  app/
    main.py               # FastAPI app factory
    config.py             # pydantic-settings, валидация при старте
    db/                   # engine, session, base, миграции alembic/
    models/               # SQLAlchemy ORM
    schemas/              # Pydantic DTO (API) — отдельно от ORM
    api/                  # роутеры: auth, users, documents, analyses, exports, admin/*
    security/             # sessions, csrf, passwords, rbac/policies, ssrf, crypto
    services/             # бизнес-логика (documents, analyses, catalog, audit, settings)
    ingestion/            # validators, extractors (pdf, docx, doc), fragmenter, page mapping
    llm/                  # LLMProvider, openrouter.py, ollama.py, mock.py, http client с SSRF guard
    analysis/             # pipeline, chunking, prompts/ (версионированные), schemas, evidence/
    exports/              # pdf, docx, csv, xlsx
    jobs/                 # очередь, worker loop, handlers, reaper
    logging.py            # structlog + redaction
  tests/                  # unit/, integration/, fixtures/ (генерируемые документы)
  pyproject.toml
converter/                # Dockerfile + маленький HTTP-сервис вокруг soffice
frontend/
  src/ (api/, features/, components/, routes/, i18n/ru.ts)
deploy/                   # backup/restore скрипты (compose.yaml — в корне, nginx.conf — в frontend/)
docs/
```

## 5. Модель данных

Все таблицы: `id UUID PK`, `created_at`, `updated_at` (timestamptz, UTC). Мягкое удаление только там, где указано. Внешние ключи с явным `ON DELETE`.

### 5.1 Пользователи и доступ

**User** — `email` (unique, lower-case), `display_name`, `password_hash` (Argon2id), `role` (`superadmin|admin|analyst|viewer`), `is_active`, `must_change_password`, `failed_login_count`, `locked_until`, `password_changed_at`, `last_login_at`, `created_by_id`.

**Session** — `user_id`, `token_hash` (HMAC-SHA256 с `SECRET_KEY` от случайного 256-битного токена; сам токен хранится только в cookie), `csrf_token_hash`, `created_at`, `last_seen_at`, `expires_at` (абсолютный TTL), `idle_expires_at` (скользящий TTL), `revoked_at`, `revoked_reason`, `ip`, `user_agent` (усечённый).

**DocumentAccess** — `document_id`, `user_id`, `permission` (`view|analyze`), `granted_by_id`. Явный доступ для Viewer и для совместной работы.

**UserModelPermission** — `user_id`, `model_config_id`, `allowed` (bool). Персональные разрешения/запреты поверх ролевых (`ModelConfig.allowed_roles`).

**ConsentRecord** — `user_id`, `provider_config_id`, `notice_version`, `accepted_at`. Факт ознакомления с предупреждением о передаче текста внешнему провайдеру. Дополнительно согласие подтверждается в каждом запросе на облачный анализ и сохраняется в `Analysis`.

### 5.2 Документы

**Document** — `owner_id`, `title`, `status` (`active|deleting`), `deleted_at`, `current_version_id`.

**DocumentVersion** — `document_id`, `version_no`, `original_filename` (только для отображения, санитизированный), `detected_format` (`pdf|docx|doc`), `mime_type` (определённый сервером), `size_bytes`, `sha256`, `storage_key` (случайный UUID-путь), `converted_storage_key` (DOCX после конвертации DOC), `extraction_status` (`pending|processing|ready|failed|unsupported_no_text`), `extraction_error_code`, `extractor_version`, `page_count`, `page_source` (см. §8.4), `canonical_text_storage_key`, `warnings` (JSONB), `uploaded_by_id`.

**DocumentPage** — `version_id`, `page_no` (1-based), `char_start`, `char_end` (смещения в каноническом тексте версии), `source` (`pdf_native|docx_rendered_breaks|libreoffice_render`).

**DocumentFragment** — `version_id`, `seq` (порядковый номер в версии, 0-based), `fragment_key` (стабильный человекочитаемый ключ `F{seq:06d}`; уникален в версии), `kind` (`heading|paragraph|table_row|table_cell|list_item|footnote|header_footer`), `text` (как извлечено, без нормализации), `char_start`, `char_end` (в каноническом тексте), `page_start`, `page_end` (nullable — только если достоверно известны), `clause_ref` (nullable, например `4.2`; только при достоверном определении), `heading_path` (JSONB: цепочка заголовков разделов), `table_ref` (JSONB: таблица/строка/ячейка), `meta` (JSONB: стиль, bbox для PDF), `text_sha256`.

Стабильность ID: фрагменты неизменяемы после извлечения. Повторное извлечение (новая версия экстрактора) создаёт новую `DocumentVersion` или явную операцию re-extract, которая запрещена при наличии анализов на этой версии. Доказательства всегда ссылаются на `fragment.id` конкретной версии.

### 5.3 Анализ

**Analysis** — `document_id`, `version_id`, `created_by_id`, `status` (`queued|running|verifying|completed|completed_with_warnings|failed|cancelling|cancelled`), `party` (`party_a|party_b|named|unknown`), `party_name`, `categories` (массив кодов), `depth` (`quick|standard|deep`), `model_config_id`, `config_snapshot` (JSONB: провайдер, тип, base_url-хост, external model id, generation params, контекст, версии промптов и схем, версия чанкера и нормализатора — **без секретов**), `cloud_transfer` (bool), `cloud_consent_at`, `idempotency_key` (unique вместе с `created_by_id`), `progress` (JSONB: этап, чанков всего/готово), `summary` (JSONB), `coverage` (JSONB: обработанные разделы/фрагменты, пропуски), `usage` (JSONB: токены/стоимость — только если вернул провайдер), `error_code`, `error_message_safe`, `started_at`, `finished_at`, `cancel_requested_at`.

**AnalysisChunk** — `analysis_id`, `chunk_index`, `chunk_key` (детерминированный хеш), `fragment_seq_start`, `fragment_seq_end`, `overlap_from_seq`, `status` (`pending|running|done|failed|skipped`), `attempts`, `result` (JSONB, валидированный ответ модели), `error_code`, `tokens_in/out`.

**AnalysisRisk** — `analysis_id`, `risk_key` (`R-001`…), `kind` (`clause_risk|missing_clause|contradiction`), `title`, `category`, `severity` (`critical|high|medium|low|informational`), `description`, `reason`, `consequences`, `affected_party`, `recommendation`, `suggested_revision` (nullable, всегда помечается как проект), `model_confidence` (0–1, оценка модели), `validation_status` (`verified_match|needs_review|rejected`), `validation_notes` (JSONB), `search_scope` (JSONB — только для `missing_clause`: проверенные разделы/фрагменты, ограничения), `source_chunk_ids`, `dedup_group`, `is_hidden_by_user` (не удаляет данные).

**RiskEvidence** — `risk_id`, `fragment_id` (FK на реальный фрагмент той же версии), `fragment_ids_span` (для цитат через границу соседних фрагментов), `quote` (как вернула модель), `quote_normalized`, `match_status` (`exact|normalized|not_found|fragment_mismatch|too_short`), `match_char_start`, `match_char_end` (в каноническом тексте), `page_start`, `page_end`, `clause_ref` (взяты с сервера из метаданных фрагмента), `relevance_status` (`supports|partially_supports|does_not_support|unchecked`), `relevance_rationale`, `relevance_checked_by` (`verifier_llm|rules`), `verifier_model_snapshot`.

### 5.4 Каталог моделей и настройки

**ProviderConfig** — `name`, `type` (`openrouter|ollama`), `base_url`, `is_cloud` (true для OpenRouter; для Ollama — по признаку адреса/явной настройке), `enabled`, `secret_ciphertext` (nullable), `secret_key_id`, `secret_last4`, `allow_private_network` (только Superadmin, для доверенной локальной Ollama), `timeout_s`, `max_concurrency`, `extra_headers` (без секретов), `last_check_at`, `last_check_status`, `last_check_error_safe`, `version` (оптимистическая блокировка).

**ModelConfig** — `provider_config_id`, `display_name`, `external_model_id`, `enabled`, `purpose` (массив: `analysis|verifier|structure`), `priority`, `is_default` (частичный unique-индекс на одну модель по назначению), `allowed_roles`, `context_window` (nullable — если известно), `max_output_tokens`, `generation_params` (JSONB: temperature, top_p, seed…), `structured_output` (`json_schema|json_mode|none|unknown` — `json_schema` только если подтверждено проверкой), `pricing` (JSONB: значения + `source` (`provider_api|admin_manual`) + `fetched_at`; если неизвестно — `null`, UI показывает «неизвестно»), `last_tested_at`, `last_test_status`, `version`.

**ConfigRevision** — история изменений провайдеров/моделей/настроек: `entity_type`, `entity_id`, `revision`, `diff` (JSONB, секреты заменены на `***`), `changed_by_id`.

**SystemSetting** — `key`, `value` (JSONB), `updated_by_id`. Ключи: `cloud_models_enabled`, `cloud_notice_version`, `cloud_notice_text`, `max_upload_mb`, `max_pages`, `max_concurrent_analyses_per_user`, `max_analysis_tokens`, `retention_days` (null = бессрочно), `bootstrap_completed`, `default_depth`.

### 5.5 Задачи, аудит, экспорт

**Job** — `type` (`extract_document|run_analysis|build_export|purge_document|refresh_model_catalog`), `payload` (JSONB, только ID), `status` (`queued|running|succeeded|failed|cancelled|dead`), `priority`, `attempts`, `max_attempts`, `run_after`, `locked_by`, `lease_expires_at`, `heartbeat_at`, `last_error_code`, `dedup_key` (unique для активных задач).

**Export** — `analysis_id`, `requested_by_id`, `format` (`pdf|docx|csv|xlsx`), `status`, `storage_key`, `expires_at`.

**AuditLog** — append-only: `actor_user_id`, `action` (`auth.login_success`, `auth.login_failed`, `user.role_changed`, `model.updated`, `document.uploaded`, `analysis.started`, `document.deleted`, `export.downloaded`, …), `target_type`, `target_id`, `ip`, `request_id`, `details` (JSONB, без паролей, ключей и текста договоров), `created_at`. Приложение не выполняет UPDATE/DELETE над этой таблицей (в prod — отдельная роль БД без этих прав).

**WorkerHeartbeat** — `worker_id`, `last_seen_at`, `version`.

## 6. Права доступа

### 6.1 Роли

| Возможность | Superadmin | Admin | Analyst | Viewer |
|---|---|---|---|---|
| Управление пользователями | все роли | analyst, viewer, admin*; не может назначать/изменять superadmin | — | — |
| Провайдеры: секреты, `allow_private_network`, base_url OpenRouter | да | нет | — | — |
| Модели: включение, роли, разрешения пользователям | да | да | — | — |
| Системные настройки (облако вкл/выкл, лимиты, retention) | да | только чтение | — | — |
| Журнал аудита | да | да | — | — |
| Загрузка документов | да | да | да | нет |
| Свои документы: чтение, анализ, удаление, выдача доступа | да | да | да | — |
| Чужие документы | да (системный доступ, действие аудируется) | только при явном `DocumentAccess` | только при явном `DocumentAccess` | только `view` при явном доступе |
| Запуск анализа | да | да | на своих и с `analyze` | нет |
| Экспорт отчёта | на доступных | на доступных | на доступных | на доступных (view) |

\* Admin не может повысить кого-либо до superadmin, изменить/заблокировать superadmin, изменить собственную роль. Последнего активного Superadmin нельзя понизить или заблокировать.

Решение: Admin управляет пользователями и моделями, но **не читает содержимое чужих договоров** без явного доступа (принцип минимальных привилегий; администрирование ≠ доступ к конфиденциальным данным).

### 6.2 Принудительное применение

- Единый модуль политик `security/policies.py`: `authorize(actor, action, resource) -> None | raise`. Каждый эндпоинт вызывает политику; UI только отражает права.
- Загрузка объектов только через scoped-репозитории (`app/security/scoped.py`): `get_document_for(actor, id, action)`, `get_analysis_for(...)`, `get_export_for(...)`. Они строят запрос с условием доступа (`owner_id = actor OR EXISTS(DocumentAccess) OR actor is superadmin`), а не загружают объект и проверяют потом. Нет доступа → `404 not_found`.
- Дочерние объекты (страницы, фрагменты, риски, доказательства, экспорты) проверяются через родительский документ; ID дочернего объекта в URL всегда сочетается с ID родителя, и сервер проверяет принадлежность.
- Списки фильтруются тем же условием на уровне SQL. Список документов у всех ролей, включая Superadmin, — «свои и явно доступные»; системный доступ Superadmin к чужому документу — только по ID и с записью `document.accessed_by_superadmin` в аудит.
- Тесты IDOR: для каждого эндпоинта с ID — матрица ролей × «свой/чужой/с доступом/несуществующий».

### 6.3 Сессии и аутентификация

- Пароли: Argon2id (`argon2-cffi`, параметры по OWASP), проверка длины ≥ 12 и по списку распространённых паролей; повторное хеширование при смене параметров.
- Вход: `POST /api/auth/login` → создаёт `Session`, ставит cookie `cai_session` (`HttpOnly; Secure; SameSite=Lax; Path=/api`) и `cai_csrf` (не HttpOnly, `Path=/`, для чтения SPA на любой странице). Новая сессия при каждом входе и смене пароля (защита от фиксации). В БД — только HMAC токенов; ротация `SECRET_KEY` инвалидирует все сессии.
- TTL: idle 8 ч (скользящий, продлевается не чаще раза в минуту), absolute 7 дней (`SESSION_IDLE_MINUTES`, `SESSION_ABSOLUTE_HOURS`). Смена пароля, сброс пароля администратором или блокировка отзывают все сессии пользователя. Пользователь видит и завершает свои сессии (`/auth/sessions`), администратор — завершает все сессии пользователя.
- CSRF: для всех изменяющих методов требуется заголовок `X-CSRF-Token`, совпадающий с токеном именно этой сессии (сравнение HMAC в постоянном времени), и `Origin` (или при его отсутствии `Referer`) из `ALLOWED_ORIGINS`. `POST /auth/login` (сессии ещё нет) защищён проверкой `Origin`.
- Rate limit входа: скользящее окно в PostgreSQL (`login_attempts`, e-mail хранится как HMAC): по e-mail — `LOGIN_MAX_FAILURES_PER_EMAIL` (5) неудач за `LOGIN_WINDOW_MINUTES` (15) после последнего успешного входа, по IP — `LOGIN_MAX_FAILURES_PER_IP` (30); затем `429` с `Retry-After`. Прогрессивная задержка ответа с третьей неудачи (`LOGIN_DELAY_STEP_MS`, максимум `LOGIN_DELAY_MAX_MS`). Ответы для неизвестного e-mail и неверного пароля совпадают (включая время — проверка «пустого» хеша). Сообщение «учётная запись заблокирована» показывается только при верном пароле. Администратор может снять блокировку входа.
- `must_change_password`: до смены пароля доступны только `/auth/me`, `/auth/change-password`, `/auth/logout`, `/auth/sessions*`; остальное — `403 password_change_required`.
- Административные изменения пользователей выполняются последовательно (advisory lock) с перечитыванием актора в транзакции: права проверяются по актуальной роли, встречные изменения двух Superadmin не приводят к deadlock и не оставляют систему без Superadmin.
- Временный пароль (создание пользователя, сброс) генерируется сервером (16 символов, ≈95 бит), возвращается в ответе один раз, хранится только как хеш; пользователь обязан сменить его при первом входе.

### 6.4 Bootstrap первого Superadmin

Команда `python -m app.cli bootstrap` (и автоматически при старте backend, если `BOOTSTRAP_ENABLED=true`, по умолчанию true; переменные `BOOTSTRAP_ADMIN_*` передаются только сервису `backend`):
1. В одной транзакции с advisory lock: если существует хотя бы один superadmin — ничего не меняет (не трогает пароль/роль), пишет в лог «bootstrap пропущен».
2. Если superadmin нет и заданы `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` (проходит политику паролей), `BOOTSTRAP_ADMIN_NAME` — создаёт пользователя с `must_change_password=true`, аудит `user.bootstrap_created`.
3. Если superadmin нет и параметров нет (или они не проходят политику) — сервер стартует, в лог пишется `bootstrap_failed` с перечнем недостающих переменных (без значений), `/api/health/ready` отвечает 503 с `accounts: bootstrap_required`, страница входа показывает предупреждение; вход невозможен, открытых аккаунтов нет. Пароль по умолчанию не существует. CLI в этом случае завершается с кодом 1.
3a. Если bootstrap уже выполнялся (`system_settings.bootstrap_completed`) — повторно не выполняется, даже если Superadmin позже понижен вручную. Если пользователь с `BOOTSTRAP_ADMIN_EMAIL` уже существует — он не повышается и его пароль не меняется.
4. Пароль никогда не логируется; после инициализации рекомендуется удалить переменные из окружения и выставить `BOOTSTRAP_ENABLED=false` (при пропуске bootstrap с заданным паролем в лог пишется напоминание `bootstrap_password_still_configured`).

## 7. API

- Префикс `/api`, JSON, OpenAPI доступен только при `ENV=development` (или под авторизацией Superadmin).
- Единый формат ошибок:
  ```json
  {"error": {"code": "document_not_found", "message": "Документ не найден", "request_id": "…", "details": {}}}
  ```
  `message` безопасен для показа; стектрейсы и внутренние детали — только в логах (без содержимого договоров).
- `X-Request-ID` на каждом ответе.
- Пагинация по курсору (`?cursor=&limit=`), лимит ≤ 100.
- Идемпотентность: `Idempotency-Key` для `POST /analyses`, `POST /documents`, `POST /exports`.
- Основные ресурсы: `/auth/*`, `/users/*`, `/documents`, `/documents/{id}/versions/{vid}/{pages|fragments|file}`, `/documents/{id}/access`, `/analyses`, `/analyses/{id}/{risks|cancel|retry|events}`, `/analyses/{id}/exports`, `/exports/{id}/download`, `/models/available`, `/admin/{providers|models|settings|audit}`.
- Прогресс анализа: polling `GET /analyses/{id}` (основной путь) и SSE `GET /analyses/{id}/events` (опционально, этап 07).

## 8. Загрузка и извлечение документов

### 8.1 Приём файла (API)

1. Проверка прав (роль ≥ analyst) и лимитов (размер из `max_upload_mb`, по умолчанию 25 МБ; проверяется потоково, без чтения целиком в память сверх лимита).
2. Имя файла используется только для отображения: санитизация, без использования в путях.
3. Определение формата по сигнатуре: PDF `%PDF-`; DOCX — ZIP с `[Content_Types].xml` и `word/document.xml`, без `vbaProject.bin` и без macro-enabled content type; DOC — OLE2 `D0 CF 11 E0 A1 B1 1A E1` с потоком `WordDocument`. Расширение и заявленный `Content-Type` должны соответствовать определённому формату, иначе `415 unsupported_file_type`.
4. Сохранение во временный файл → вычисление SHA-256 → атомарное перемещение в `storage/originals/{uuid}` (каталог вне web-root, права 0600, файл не исполняемый).
5. Создание `DocumentVersion(extraction_status=pending)` и задачи `extract_document`; ответ `202` с ID.

### 8.2 Извлечение (worker)

Выполняется в дочернем процессе worker'а с таймаутом и `RLIMIT_AS`/`RLIMIT_CPU` (Linux), чтобы зависший/вредоносный файл не ронял worker.

- **ZIP-проверки (DOCX)** до открытия python-docx: число записей ≤ 2000, суммарный распакованный размер ≤ 200 МБ, коэффициент сжатия ≤ 100, запрет абсолютных путей и `..` в именах, XML разбирается без внешних сущностей (`defusedxml`/lxml с `resolve_entities=False`).
- **PDF (PyMuPDF)**: отказ для зашифрованных; `page_count ≤ max_pages` (по умолчанию 500); текст по блокам/строкам с `page_no` и bbox; удаление повторяющихся колонтитулов помечается флагом, не удаляет текст из канонической версии. Если на всех страницах меньше порога символов → `unsupported_no_text` с понятным сообщением (OCR не поддерживается).
- **DOCX (python-docx + обход XML тела)**: абзацы и таблицы в порядке документа (итерация по `w:body` с обработкой `w:p` и `w:tbl`), таблицы построчно с координатами ячеек, сноски; автонумерация списков вычисляется из `numbering.xml` для типовых форматов (decimal, многоуровневые `1.2.3`), иначе `clause_ref` не заполняется.
- **DOC**: worker отправляет файл в `converter` (HTTP в изолированной сети), получает DOCX, сохраняет как `converted_storage_key`, дальше — путь DOCX. Предупреждение «формат DOC сконвертирован, возможны расхождения форматирования».

### 8.3 Конвертер

- Контейнер с LibreOffice `--headless --norestore --nolockcheck`, на каждый запрос — новый временный профиль (`-env:UserInstallation=file:///tmp/…`), конфигурация с `MacroSecurityLevel=3` (макросы запрещены), Java отключена.
- Лимиты: таймаут 60 с с убийством группы процессов, `mem_limit`, `cpus`, `pids_limit`, `read_only: true` + tmpfs, `cap_drop: ALL`, `no-new-privileges`, сеть `internal: true` (нет интернета, нет доступа к БД).
- Одновременно один процесс soffice на контейнер (очередь на стороне worker'а).
- Принимает только DOC (для конвертации в DOCX) и DOCX (для рендера в PDF для разметки страниц).

### 8.4 Страницы и пункты — без выдумывания

| Формат | Источник страниц | Достоверность |
|---|---|---|
| PDF | физические страницы файла (`pdf_native`) | точная |
| DOCX/DOC | 1) `w:lastRenderedPageBreak` и явные разрывы страниц, сохранённые Word (`docx_rendered_breaks`), если они покрывают документ; 2) иначе — рендер в PDF через converter и сопоставление текста фрагментов со страницами (`libreoffice_render`) | приблизительная; UI и отчёт подписывают «стр. по разметке LibreOffice, может отличаться от Word» |
| если сопоставить не удалось | `page_start = null` + предупреждение | страница не указывается |

`clause_ref` заполняется только если номер явно присутствует в тексте заголовка/абзаца (`4.2.`, `Статья 5`, `Раздел 3`) или однозначно вычислен из автонумерации. Иначе — `null`, и в отчёте пункт не указывается.

### 8.5 Канонический текст и фрагменты

- Канонический текст версии — конкатенация `fragment.text` через `\n\n`; хранится файлом и используется для смещений. Он не нормализуется (кроме приведения переводов строк к `\n`), чтобы цитаты сверялись с тем, что реально извлечено.
- Фрагмент — абзац, пункт списка, строка таблицы или заголовок. Длинные абзацы (> ~2000 символов) делятся по границам предложений с сохранением непрерывных смещений.

### 8.6 Реализация (этап 04): уточнения

- **Приём файла**: тело `POST /api/documents` — сам файл (без multipart-парсера), имя — заголовок `X-File-Name` (percent-encoded UTF-8), заявленный тип — `Content-Type`, повтор — `Idempotency-Key` (уникален в паре с владельцем). Лимит проверяется по `Content-Length` до чтения и потоково при чтении. Лимиты для подсказок UI — `GET /api/documents/upload-limits`.
- **DOCX** читается напрямую (`zipfile` + `lxml` с парсером без сущностей/DTD/сети/huge_tree; `<!DOCTYPE`/`<!ENTITY>` отклоняются), а не через python-docx: под контролем и настройки парсера, и набор читаемых частей (document, styles, numbering, footnotes/endnotes, `docProps/app.xml`). python-docx используется только для генерации тестовых фикстур.
- **Строка таблицы** — один фрагмент `table_row`: ячейки через « | », ячейки также в `meta.cells`; координаты — `table_ref` (`table`, `row`, `columns`, `parent` для вложенных). Колонтитулы DOCX и надписи (text box) не извлекаются — предупреждение. Удалённый текст исправлений не включается — предупреждение `tracked_changes_applied`.
- **Страницы DOCX**: `docx_rendered_breaks` используется, только если число `w:lastRenderedPageBreak` + 1 равно `Pages` из `docProps/app.xml` (разметка Word согласована); иначе рендер DOCX → PDF в converter и поиск текста фрагментов в тексте страниц (сравнение только букв/цифр без регистра, монотонно). Не найденный фрагмент остаётся без страницы. Разрыв страницы внутри абзаца сохраняется в `meta.page_breaks` (`[[смещение, страница]]`).
- **Смещения** `char_start/char_end` — индексы символов Unicode (code points) в каноническом тексте, а не UTF-16.
- **Изоляция разбора**: `python -E -s -B -m app.ingestion.sandbox <mode> <path>` с окружением только `PATH`/`LANG`; дочерний процесс сам ставит `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_CORE` (Linux); родитель убивает группу процессов по таймауту. Ошибки разбора возвращаются кодами, stderr не логируется (там может оказаться текст документа).
- **Converter**: `POST /convert?from=doc&to=docx` и `?from=docx&to=pdf`; новый каталог и профиль LibreOffice на запрос (`MacroSecurityLevel=3`, `DisableMacrosExecution`, `BlockUntrustedRefererLinks`), один soffice одновременно (очередь до 60 с → 429), таймаут `LIBREOFFICE_TIMEOUT_SECONDS` с `killpg`, `RLIMIT_CPU`/`RLIMIT_FSIZE`.
- **API текста**: `GET /documents/{id}/versions/{vid}` (метаданные, предупреждения), `/pages`, `/fragments?page=&after_seq=&limit=` (курсор по `seq`), `/file` (оригинал); `POST .../retry-extraction` (только для `failed`); `GET|PUT /documents/{id}/access`, `DELETE /documents/{id}/access/{user_id}` — выдача доступа только владельцем (Superadmin не делится чужими документами).

## 9. Конвейер анализа

### 9.1 Запуск (API, синхронно)

1. Политика: доступ `analyze` к документу; версия в статусе `ready`.
2. Модель: включена, провайдер включён, доступна роли/пользователю; если `is_cloud` — `cloud_models_enabled=true` и в запросе `cloud_consent=true` (иначе `409 cloud_consent_required`).
3. Лимиты: число активных анализов пользователя; оценка токенов (`len(text)/3.5` × коэффициент глубины + проходы) ≤ `max_analysis_tokens`; если цена модели известна — оценка стоимости показывается, но не выдумывается при неизвестной цене.
4. Идемпотентность по `Idempotency-Key`: повторный запрос возвращает тот же анализ.
5. Создание `Analysis` со snapshot конфигурации, `AnalysisChunk` и задачи `run_analysis`. Аудит `analysis.started` (без текста).

### 9.2 Этапы (worker)

```
plan → structure → chunk_analysis (N) → cross_section → dedup → evidence_match → evidence_relevance → (retry_unverified) → coverage → report
```

1. **Чанкинг.** Последовательные фрагменты группируются в чанки по бюджету токенов модели (`context_window` − системный промпт − резерв на вывод; если контекст неизвестен — консервативные 8k). Перекрытие — последние фрагменты предыдущего чанка общей длиной ~15% бюджета (не менее одного фрагмента). Фрагмент не режется между чанками. `chunk_key = sha256(version_id, seq_start, seq_end, chunker_version, model_ctx)` — повторный запуск даёт те же чанки.
2. **Структура.** Отдельный вызов (или детерминированно из заголовков, если модель недоступна для этого назначения) строит оглавление: разделы → диапазоны фрагментов, стороны договора, предмет. Используется для контекста и проверки полноты.
3. **Анализ чанков.** Каждый чанк — отдельный вызов с: сторона анализа, категории, краткая структура договора, фрагменты в формате `<fragment id="F000123" page="4">…</fragment>`. Модель возвращает риски строго по JSON-схеме (§9.3). Результат валидируется Pydantic и сохраняется в `AnalysisChunk.result` — поэтому после сбоя продолжается с первого незавершённого чанка.
4. **Межраздельный проход.** На вход — сжатые карточки рисков и ключевые условия (сроки, суммы, ответственность, порядок расторжения) с фрагментами-источниками; поиск противоречий и отсутствующих условий (`missing_clause`), с явным перечнем проверенных разделов.
5. **Дедупликация.** Риски из перекрытий и соседних чанков объединяются по (категория, пересечение fragment_id доказательств, близость заголовков — нормализованное сходство); при объединении сохраняются все доказательства, severity — максимальная, заметка о слиянии.
6. **Проверка доказательств** (§10).
7. **Повтор для неподтверждённых.** Для рисков с `not_found`/`fragment_mismatch` — один повторный запрос к модели с исходными фрагментами и просьбой дать точную цитату или отозвать риск. После этого — окончательная классификация.
8. **Полнота.** Покрытие: все ли чанки `done`, какие разделы структуры не попали ни в один успешный чанк; результат — в `Analysis.coverage`, при пропусках статус `completed_with_warnings`.
9. **Отчёт.** Сводка: число рисков по severity и статусу проверки, сторона, модель/snapshot, охваченные категории и разделы, ограничения. Никогда не формулируется как «договор безопасен»; при отсутствии подтверждённых рисков — «в проанализированных разделах модель не выявила подтверждённых рисков; это не гарантирует отсутствие рисков».

Если сторона `unknown`: промпт требует описывать риски нейтрально («для стороны, принимающей обязательство…»), `affected_party` из набора `party_a|party_b|both|unclear`, и запрещает оценки «выгодно/невыгодно для вас».

### 9.3 JSON-схема ответа анализа чанка

Pydantic-модели с `extra="forbid"`, ограничениями длины и перечислениями; из них генерируется JSON Schema для `response_format`/`format`.

```json
{
  "risks": [
    {
      "title": "string ≤ 200",
      "category": "payment|liability|penalties|deadlines|renewal|termination|unilateral_changes|warranties|confidentiality|personal_data|intellectual_property|governing_law|force_majeure|acceptance|undefined_terms|contradiction|missing_clause|other",
      "kind": "clause_risk|missing_clause|contradiction",
      "severity": "critical|high|medium|low|informational",
      "description": "string ≤ 2000",
      "reason": "string ≤ 2000",
      "consequences": "string ≤ 2000",
      "affected_party": "party_a|party_b|both|unclear",
      "recommendation": "string ≤ 2000",
      "suggested_revision": "string ≤ 3000 | null",
      "model_confidence": 0.0,
      "evidence": [
        {"fragment_id": "F000123", "quote": "string 10..1500"}
      ],
      "search_scope": "string ≤ 1000 | null"
    }
  ],
  "notes": "string ≤ 1000 | null"
}
```

Правила, проверяемые сервером, а не только схемой: `fragment_id` должен входить в фрагменты этого чанка; для `clause_risk` и `contradiction` — минимум одно доказательство (для contradiction — минимум два из разных фрагментов); для `missing_clause` доказательства не обязательны, `search_scope` обязателен; поля страниц/пунктов в ответе модели отсутствуют — их подставляет сервер; ссылки на нормы права допускаются только как текст рекомендации и помечаются в UI «требует проверки юристом» — сервер не подтверждает существование норм.

Невалидный JSON → одна попытка «repair» (тот же вызов с сообщением об ошибке валидации, без текста договора в логах) → иначе чанк `failed` с `error_code=invalid_model_output`.

### 9.4 Реализация (этап 06): уточнения

- **Код**: `app/analysis/` — `chunking.py` (планирование чанков), `structure.py` (разделы, ключевые фрагменты, стороны), `prompts.py` (версионированные промпты, экранирование), `schemas.py` (схемы ответов модели и JSON Schema для провайдера), `quotes.py` (нормализация и поиск цитат), `evidence.py` (шаг A, правила шага B, итоговый статус), `dedup.py`, `injection.py`, `report.py` (покрытие, сводка), `runner.py` (исполнитель этапов), `errors.py` (безопасные сообщения); сервис `app/services/analyses.py`, API `app/api/analyses.py`, задача `run_analysis` — `app/jobs/handlers/analyses.py`. Миграция `0004_analysis_pipeline` (неразрушающая): `analyses.stage_results` (результаты этапов вне чанков), `risk_evidence.attempt`, индекс `(created_by_id, status)`.
- **API**: `POST /analyses/estimate` (оценка без запуска), `POST /analyses` (`Idempotency-Key`), `GET /analyses?document_id=&status=&cursor=`, `GET /analyses/{id}` (статус, прогресс, snapshot, сводка, покрытие, расход), `GET /analyses/{id}/risks?validation_status=&severity=&kind=&category=&include_rejected=`, `POST /analyses/{id}/cancel`, `POST /analyses/{id}/retry`. Запуск, отмена и повтор — роль Analyst+ (иначе 403) и доступ `analyze` к документу (иначе 404); чтение — доступ `view`. Внутренние результаты этапов (`stage_results`, ответы модели по чанкам) наружу не отдаются.
- **Запуск** (одна транзакция: анализ, чанки, задача, аудит `analysis.started`): документ и версия (`ready`) через scoped-запросы; модель — `resolve_model_for_use(purpose=analysis)`, модель-верификатор (необязательно, `purpose=verifier`); для облачного провайдера — выключатель облака, подтверждение в запросе (`cloud_consent`) **и** `ConsentRecord` на текущую версию предупреждения (иначе 409 `cloud_notice_not_accepted`); оценка токенов ≤ `ANALYSIS_MAX_TOKENS` (422 `analysis_budget_exceeded`); активных анализов ≤ `ANALYSIS_MAX_ACTIVE_PER_USER` (429, проверка под advisory lock пользователя); `Idempotency-Key`: тот же запрос → 200 + `Idempotent-Replayed`, другой запрос с тем же ключом → 409 `idempotency_key_reused`. Snapshot: модель и провайдер (`config_snapshot`), верификатор, версии конвейера/схемы/промптов/чанкера/нормализатора/детектора, параметры чанкинга, лимиты, оценка, сведения о версии документа.
- **Сторона**: `party_a` — первая сторона, названная в преамбуле, `party_b` — вторая (имена берутся из структуры, только если найдены в тексте договора), `named` — название пользователя (экранируется в промпте), `unknown` — нейтральные формулировки; если в тексте риска при `unknown` есть обращение к читателю («для вас»), риск получает отметку `party_wording_not_neutral`.
- **Глубина**: `quick` — без вызова модели для структуры, до 12 000 токенов текста на чанк, до 8 рисков на чанк; `standard` — 8 000 и 15; `deep` — 4 000 и 25.
- **Чанкинг** (`chunker-1`): `CHARS_PER_TOKEN = 3` (а не 3,5 из §9.1 — консервативнее для кириллицы); бюджет текста = контекст (или 8192) − 2500 (инструкции) − лимит ответа, но не больше предела глубины; < 1000 токенов → 422 `model_context_too_small`. Перекрытие ≤ 15 % бюджета, не меньше одного фрагмента; фрагменты перекрытия помечаются в промпте `context="previous"`.
- **Структура**: разделы — детерминированно (заголовки и короткие пункты с номером верхнего уровня; иначе блоки по 40 фрагментов); модель (кроме `quick`) — вид договора, предмет, стороны по заголовкам и началу/концу текста; сбой модели здесь не останавливает анализ.
- **Схемы ответа** (`schema-1`): Pydantic с `extra="forbid"`; провайдеру передаётся JSON Schema с раскрытыми `$ref`, всеми полями в `required` (nullable — `anyOf` с `null`) и `additionalProperties: false`, без ограничений длины и диапазонов — их проверяет сервер (strict-режим OpenAI/OpenRouter принимает такую схему — проверено live). Ответ не по схеме → одна попытка repair (в сообщении только пути и типы ошибок, без значений), затем ошибка этапа; `output_truncated` не повторяется.
- **Межраздельный проход** — только если выбраны `contradiction` или `missing_clause`: на вход заголовки разделов, ключевые фрагменты (сроки, суммы, ответственность, расторжение, подсудность и т.п. — по словарю, в пределах бюджета чанка) и краткие карточки уже найденных рисков. Доказательства принимаются только из переданных фрагментов.
- **Категории**: риски вне выбранных категорий отбрасываются (`missing_clause`/`contradiction` по `kind` проходят, если выбраны соответствующие категории); число отброшенных — в `stage_results.dedup`.
- **Дедупликация** — см. `dedup.py`: тип + категория + общий фрагмент и сходство названий; противоречия сравниваются без категории; из вложенных цитат одного фрагмента остаётся более полная.
- **Шаг A** (`normalizer-1`): «фрагменты, которые видела модель» — фрагменты её запроса (чанк с перекрытием или набор межраздельного прохода; для объединённого риска — объединение). Фрагмент не существует или не был в запросе → `fragment_mismatch`; цитата только в соседнем фрагменте → `not_found` с подсказкой `quote_found_in:F…`. К нормализации §10.1 добавлены: одинарные кавычки `‘’‚'` как класс; перенос слова по слогам (дефис в конце строки между строчными буквами) — только если без этого цитата не найдена; сущности `&lt;`/`&gt;`/`&amp;` (так текст экранирован в промпте). Страницы — по смещениям совпадения и `document_pages`; пункт — `clause_ref` фрагмента, с которого начинается цитата.
- **Повтор цитаты**: для риска без доказательств или с любым доказательством `not_found`/`fragment_mismatch`/`too_short` — один запрос с фрагментами, которые модель видела; `withdraw` → риск отклоняется; новые доказательства (`attempt=1`) заменяют прежние (прежние остаются с пометкой `superseded`). Не больше `ANALYSIS_QUOTE_RETRY_LIMIT` на анализ.
- **Шаг B**: один вызов верификатора на риск со всеми совпавшими цитатами; цитата — дословный текст документа по смещениям (а не текст модели), контекст — фрагменты ±1; отдельный контекст без истории анализа (`verifier-2`: для противоречия каждая цитата оценивается как одно из противоречащих условий). Недоступен/ответ не по схеме → `unchecked` (риск — `needs_review`, анализ — `completed_with_warnings`).
- **Итоговый статус** (уточнение §10.3): `missing_clause` → всегда `needs_review`; отозван моделью, нет доказательств, есть `fragment_mismatch`, нет совпавших цитат или все совпавшие `does_not_support` → `rejected`; часть цитат не совпала, часть `does_not_support`, нет ни одного `supports` или у противоречия меньше двух подтверждающих фрагментов → `needs_review`; иначе `verified_match`. Причины — в `validation_notes.status_reasons`. Отметки: `legal_reference_unverified` (упоминание норм права), `merged_from`, `from_cross_section`, `heading_quote_capped`.
- **Восстановление**: результаты этапов в `stage_results`, чанков — в `analysis_chunks`; каждая запись под блокировкой строки анализа с проверкой `progress.run_id` (старый исполнитель после перехвата задачи ничего не запишет); задача — до 4 попыток; временная ошибка провайдера → повтор задачи (на последней — `failed`), неустранимая → `failed`; worker закрывает анализы без активной задачи (`job_lost`). Перед каждым вызовом модели проверяются отмена и (для облака) выключатель облака и включённость провайдера; во время вызова отмена проверяется каждые `ANALYSIS_CANCEL_POLL_SECONDS` и прерывает HTTP-запрос. Если провайдер стал облачным после запуска без облачного согласия — `failed` (`cloud_consent_missing`).
- **Расход**: токены и стоимость — только из ответа провайдера (`usage`); при превышении `ANALYSIS_MAX_TOKENS` во время работы оставшиеся этапы пропускаются с предупреждением. Для Ollama сервер задаёт `num_ctx` по размеру запроса (не больше контекста модели).

## 10. Проверка доказательств

### 10.1 Шаг A — точное совпадение (детерминированно)

1. `fragment_id` → реальный `DocumentFragment` той же версии (иначе `fragment_mismatch`; несуществующий ID → риск не может быть `verified_match`).
2. Нормализация (документирована, версия `normalizer_version`), применяется одинаково к цитате и тексту фрагмента и **не меняет слова**:
   - Unicode NFC;
   - CR/LF, табуляции, NBSP (`U+00A0`), узкие пробелы (`U+2009`, `U+202F`) → пробел; последовательности пробелов → один пробел; обрезка по краям;
   - удаление мягкого переноса `U+00AD` и zero-width символов;
   - эквивалентность типографских кавычек (`«»„“”"`) и тире (`‐‑–—−-`) — только как классы символов.
   Регистр, буквы, цифры, знаки препинания (кроме перечисленных классов) не нормализуются.
3. Поиск нормализованной цитаты в нормализованном тексте фрагмента; если не найдено — в объединении фрагмента с соседними (±1) того же чанка (цитата через границу абзаца/страницы).
4. Результат: `exact` (совпало без нормализации) / `normalized` (совпало после нормализации) / `not_found` / `too_short` (< 20 символов или < 4 слов — тривиальная цитата не доказывает ничего). Сохраняются смещения в каноническом тексте (обратное отображение нормализованных позиций в исходные).
5. Страница и пункт вычисляются сервером по смещениям и метаданным фрагментов; всё, что модель могла написать о страницах, игнорируется.

### 10.2 Шаг B — смысловая поддержка (отдельно)

- Отдельный вызов модели-верификатора (назначение `verifier`; по умолчанию та же модель, можно выбрать другую), без истории анализа: на вход — формулировка риска (title, reason, category, kind), цитата и окружающий контекст (фрагмент ±1). Вопрос: подтверждает ли цитата именно это утверждение? Ответ по схеме: `supports|partially_supports|does_not_support` + краткое обоснование.
- Детерминированные правила поверх: цитата целиком из заголовка/оглавления → не выше `partially_supports`; для contradiction — доказательства должны быть из разных фрагментов.
- Если верификатор недоступен — `relevance_status=unchecked`.

### 10.3 Итоговый статус риска

| Условие | `validation_status` |
|---|---|
| Все обязательные доказательства `exact/normalized` и хотя бы одно `supports`, ни одно не `does_not_support` | `verified_match` |
| Совпадение есть, но связь `partially_supports` или `unchecked` | `needs_review` |
| `missing_clause` (нет цитаты по определению) | `needs_review` + отдельный тип отображения с `search_scope` |
| После повтора нет ни одного совпавшего доказательства, фиктивный `fragment_id`, или все совпавшие `does_not_support` | `rejected` |

UI и отчёт: `verified_match` — «цитата найдена в тексте и подтверждает риск по оценке верификатора»; `needs_review` — «требует проверки»; `rejected` — скрыты по умолчанию, доступны фильтром «отклонённые», никогда не показываются как установленный факт.

## 11. Защита от prompt injection

- Текст договора передаётся только в user-сообщении внутри разделителей `<fragment …>` с экранированием `<`/`>` в содержимом; системный промпт явно указывает, что содержимое фрагментов — данные, инструкции в них не выполняются.
- Модель не получает секретов, системных настроек, данных других пользователей; у неё нет инструментов (tool calling выключен) — единственный эффект ответа — JSON, который сервер валидирует.
- Строгая схема с `extra="forbid"`, enum-категориями, ограничениями длины; любые поля вне схемы отбрасываются, ответ без схемы — ошибка.
- Модель не может повлиять на статус проверки, страницы, пункты, права, модель и параметры: всё это вычисляет сервер.
- Эвристический детектор подозрительных инструкций в тексте («игнорируй предыдущие инструкции», «ignore previous», роли `system:` и т.п.) → предупреждение в отчёте и в `Analysis.summary.warnings`, без изменения поведения конвейера.
- Верификатор работает в отдельном контексте, чтобы инъекция в анализе не «подтвердила» сама себя.
- Вывод модели в UI отображается как текст (React экранирует; `dangerouslySetInnerHTML` запрещён линтером oxlint `react/no-danger`; Markdown не рендерится), в PDF/DOCX экспорт — с экранированием разметки.

## 12. Слой LLM-провайдеров

```python
class LLMProvider(Protocol):
    async def generate_structured(self, req: StructuredRequest) -> StructuredResult: ...
    async def check_connection(self) -> ProviderHealth: ...
    async def list_models(self) -> list[RemoteModelInfo]: ...

StructuredRequest: messages, json_schema, schema_name, model, params (temperature, max_tokens, seed), timeout
StructuredResult: parsed_json, raw_text_len, usage: Usage | None (prompt/completion tokens, cost — только если вернул провайдер), provider_request_id, finish_reason
```

- Ошибки нормализуются в иерархию: `ProviderTimeout`, `ProviderRateLimited(retry_after)`, `ProviderAuthError`, `ModelUnavailable`, `ContextTooLong`, `InvalidModelOutput`, `ProviderUnavailable`. Бизнес-логика работает только с ними.
- Повторы: только для timeout/429/5xx, максимум 3, экспоненциальный backoff с jitter, уважение `Retry-After`; не повторяются 401/403/400.
- **OpenRouter**: `POST {base_url}/chat/completions`, ключ в `Authorization: Bearer`, `response_format` с `json_schema`, если модель подтверждённо поддерживает structured outputs, иначе JSON-режим/инструкция + валидация. Каталог моделей — `GET /models` (контекст и цены сохраняются с `source=provider_api` и датой). Точные поля API сверяются с актуальной документацией OpenRouter при реализации (этап 05).
- **Ollama**: `POST {base_url}/api/chat` с `format` (JSON-схема), `stream=false`; `GET /api/tags` — список моделей; `GET /api/version` — health. Модели не скачиваются автоматически. Документация явно объясняет: из контейнера `localhost` — это сам контейнер; использовать `http://ollama:11434` (compose profile) или адрес хоста (`host.docker.internal` / IP), разрешённый администратором.
- HTTP-клиент провайдеров: `follow_redirects=False`, SSRF-guard (THREAT_MODEL §SSRF), лимит размера ответа, таймауты connect/read раздельно, логирование только метаданных (провайдер, модель, длительность, статус, токены) — без тела запроса/ответа.
- Mock-провайдер (детерминированный, сценарии через фикстуры) используется в тестах и e2e; в production его нельзя выбрать (не регистрируется при `ENV=production`).
- Каталог моделей динамический: изменения в БД применяются без рестарта; кеш с TTL 30 с + инвалидация при изменении.

### 12.1 Реализация (этап 05): уточнения

- **Код**: `app/llm/` — `base.py` (контракт `LLMProvider`, типы), `errors.py` (иерархия ошибок), `netguard.py` (SSRF-guard и транспорт с pinned IP), `http.py` (клиент, таймауты, лимит ответа 8 МБ / 32 МБ для списка моделей, повторы), `openrouter.py`, `ollama.py`, `mock.py`, `factory.py`; сервис `app/services/catalog.py`, API `app/api/catalog.py`, шифрование `app/security/secretbox.py`. Кеша каталога нет: каждый запрос читает БД, изменения применяются сразу.
- **Сверка с документацией (2026-10-16)**. OpenRouter: `POST /chat/completions`, `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}` + `provider.require_parameters: true`; `usage.cost` берётся только если есть; ошибки после начала генерации приходят с HTTP 200 (`error` в теле или `finish_reason: "error"`) — обрабатываются как ошибка провайдера; `GET /models` — цены строками в USD за токен (пересчитываются в USD за 1 млн токенов; отрицательные значения = цена неизвестна); `GET /key` — проверка ключа без генерации. Ollama: `POST /api/chat` (`stream: false`, `format` — схема или `"json"`, `options.num_predict/num_ctx/seed`), `GET /api/version` (health), `GET /api/tags`, `POST /api/show` (`model_info["<arch>.context_length"]`); 404 — модель не скачана. `/api/pull` не вызывается никогда.
- **Структурированный вывод**: `json_schema` выставляется только после успешного тестового вызова со схемой (`POST /admin/models/{id}/test` с `generate=true`: попытки `json_schema` → `json_mode` → инструкция; тестовый запрос не содержит данных договоров). Вручную администратор может выбрать `json_mode|none|unknown`. Для `none/unknown` схема передаётся инструкцией в системном сообщении, ответ всё равно разбирается строго (весь ответ — один JSON-объект, допускается только обёртка ```json).
- **Ошибки и повторы**: 401 → `provider_auth_failed`; 402 → `provider_quota_exceeded`; 403 → `provider_forbidden`; 404 и сообщения «not a valid model / no endpoints found» → `model_unavailable`; 413 и «context length» в 400 → `context_too_long`; 429 → `provider_rate_limited` (`Retry-After` в секундах или HTTP-дате); 408/timeout → `provider_timeout`; 5xx, сетевые ошибки → `provider_unavailable`/`connection_failed`; `finish_reason=length` → `output_truncated`. Повторяются только timeout/429/5xx/сеть: до 3 повторов, backoff 1 → 2 → 4 с (×0,5–1 jitter, максимум 30 с); `Retry-After` > 60 с — без повтора (задача повторится позже). Сообщения провайдера используются только для классификации и не передаются наружу и в логи.
- **Сеть**: `GuardedTransport` на каждый запрос разрешает имя, проверяет **все** адреса и подключается к первому проверенному IP; `Host`, SNI и проверка сертификата — по исходному имени (`sni_hostname`). Запрещены всегда: metadata (169.254.169.254, 169.254.170.2, fd00:ec2::254, 100.100.100.200, 168.63.129.16, 192.0.0.192, `metadata.google.internal` и др.), link-local, multicast, unspecified, reserved, broadcast; IPv4-mapped/NAT64/6to4 разворачиваются. Loopback, RFC 1918, ULA и CGNAT — только с `allow_private_network`. `http` — только с `allow_private_network`. Любой 3xx — `unexpected_redirect`. `trust_env=False` (прокси из окружения не используются).
- **Облачность**: OpenRouter — всегда облачный; Ollama без частной сети — облачная (данные уходят во внешнюю сеть); Ollama с частной сетью — по умолчанию локальная (`is_cloud=false`), и тогда публичные адреса запрещены (`allow_public=false`): если DNS-имя локальной Ollama начнёт указывать в интернет, запрос будет заблокирован.
- **Секреты**: `MultiFernet` из `SECRETS_ENCRYPTION_KEYS` (первый ключ шифрует), в БД — шифротекст, отпечаток ключа (`secret_key_id`, первые 16 hex SHA-256 ключа) и последние 4 символа. API отдаёт только `has_secret`/`secret_last4`; в `ConfigRevision` и аудите — `***`. Смена `base_url` без нового ключа сбрасывает ключ (`X-Secret-Reset: 1`). Ротация — `python -m app.cli rotate-secrets`. Дополнительные заголовки провайдера не могут содержать секреты (имена с key/token/secret/auth/… и служебные заголовки отклоняются).
- **Права**: провайдеры (создание, адрес, ключ, частная сеть, включение, удаление) и глобальные настройки облака — только Superadmin; модели (создание, правка, импорт, проверка, удаление), проверка соединения провайдера, история изменений — Admin и Superadmin. Эндпоинты: `/api/admin/providers[/{id}[/check|/remote-models|/import-models]]`, `/api/admin/models[/{id}[/test]]`, `/api/admin/config-revisions`, `/api/admin/llm-settings`, `/api/models/available`, `/api/models/{id}/cloud-consent`.
- **Выбор модели**: `/api/models/available` и серверная `resolve_model_for_use` применяют одно правило: роль Analyst+; модель и провайдер включены; назначение совпадает; персональное правило (`UserModelPermission`) сильнее ролевого; облачные — только при `cloud_models_enabled=true` (по умолчанию **false**). Ошибки: нет доступа или модели → 404 `model_not_found`; отключена → 409 `model_disabled`; облако выключено → 409 `cloud_models_disabled`; нет подтверждения → 409 `cloud_consent_required`. Согласие: `ConsentRecord` на версию текста предупреждения (новая версия текста требует нового подтверждения) + подтверждение в каждом запросе анализа (этап 06).
- **Snapshot**: `config_snapshot(model, provider)` — провайдер (id, имя, тип, хост, облачность, версия), модель (id, external id, контекст, лимит вывода, параметры генерации, режим structured output, цена с источником, версия), время; без ключа и полного URL. Сохраняется в `Analysis.config_snapshot` при запуске анализа (этап 06).
- **Цены**: из API провайдера (`source=provider_api`, `fetched_at`) или введённые администратором (`admin_manual`); при неизвестной цене `pricing = null`, UI показывает «неизвестна». Ручная цена не перезаписывается проверкой модели. Контекст/лимит вывода из API заполняются, только если поле пустое.
- **Модель по умолчанию**: не более одной (частичный уникальный индекс, миграция `0003_model_catalog`); только включённая модель с назначением `analysis`; при выключении модели флаг снимается.

## 13. Фоновые задачи

- Очередь в таблице `Job`. Worker в цикле: `SELECT … WHERE status='queued' AND run_after<=now() ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT 1` → `running`, `lease_expires_at = now()+lease`. Во время выполнения heartbeat продлевает lease.
- Reaper (в каждом worker'е, раз в минуту): задачи с истёкшим lease → `queued` с `attempts+1` или `dead` после `max_attempts`; соответствующий анализ продолжается с сохранённых чанков.
- Отмена: `POST /analyses/{id}/cancel` → `cancel_requested_at`, статус `cancelling`; worker проверяет флаг между чанками и перед каждым вызовом LLM, отменяет текущий HTTP-запрос, переводит в `cancelled`. Уже полученные результаты сохраняются, но отчёт не формируется как завершённый.
- Повтор: `POST /analyses/{id}/retry` для `failed`/`cancelled` — новая задача на том же snapshot, завершённые чанки не пересчитываются. «Запустить заново» с другой моделью — новый `Analysis`.
- Параллелизм: `WORKER_CONCURRENCY` (по умолчанию 2), лимит на провайдера (`ProviderConfig.max_concurrency`), лимит активных анализов на пользователя.
- Graceful shutdown: SIGTERM → прекратить взятие задач, дождаться текущего шага (до таймаута), иначе lease истечёт и задачу подберёт другой worker.

## 14. Облачное согласие, хранение и удаление

- Глобальный выключатель `cloud_models_enabled` (Superadmin). При `false` облачные модели не показываются в селекторе и отклоняются сервером.
- Перед первым облачным анализом пользователь видит текст предупреждения (провайдер, что передаётся — текст фрагментов договора; провайдер может хранить/обрабатывать данные согласно своим условиям; рекомендуется проверить условия). Принятие версии текста → `ConsentRecord`. В каждом облачном анализе — повторный чекбокс в форме и `cloud_consent_at` в `Analysis`.
- В облако не передаются: имя файла, данные пользователя, метаданные системы — только текст фрагментов и инструкции.
- Удаление документа (владелец/Superadmin): статус `deleting`, документ сразу исчезает из выдачи; задача `purge_document` удаляет оригиналы, конвертированные файлы, канонический текст, экспорты, а затем строки (версии, страницы, фрагменты, анализы, риски, доказательства) в одной транзакции. В `AuditLog` остаётся только факт удаления: ID документа, SHA-256 оригинала, автор, время. Название документа в аудит не пишется (может быть конфиденциальным).
- `retention_days` (по умолчанию выключено): ежедневная задача ставит в очередь удаление документов старше срока; включается только Superadmin с подтверждением.
- Резервные копии содержат удалённые данные до своей ротации — это указывается в документации.

## 15. Логирование и наблюдаемость

- `structlog` JSON в stdout; поля: `ts`, `level`, `event`, `request_id`, `user_id`, `route`, `status`, `duration_ms`, `job_id`, `analysis_id`.
- Фильтр редактирования: удаляет/маскирует ключи `password`, `token`, `secret`, `api_key`, `authorization`, `cookie`, `text`, `quote`, `content`, `prompt`; заголовки `Authorization`/`Cookie` никогда не логируются. Тест проверяет, что известный пароль, ключ и фрагмент договора не появляются в захваченных логах.
- Исключения логируются со стектрейсом, но сообщения исключений от парсеров/провайдеров проходят через тот же фильтр и обрезаются.
- Uvicorn access log отключён — заменён собственным middleware (без query-строк с потенциальными данными).

## 16. Frontend

- React 19 + TypeScript (strict), Vite, Tailwind 4, React Router 7, TanStack Query, React Hook Form + Zod, тексты интерфейса — в `i18n/ru.ts`.
- API-клиент: `credentials: 'include'`, автоматический `X-CSRF-Token`, разбор единого формата ошибок с показом `request_id`.
- Маршрутизация с проверкой роли — только UX; защиту обеспечивает backend.
- Страницы: вход/смена пароля, обзор, договоры (список, загрузка drag-and-drop, просмотр текста по страницам/фрагментам), запуск анализа (документ/версия, сторона, категории, глубина, модель, облачное согласие), список анализов, отчёт (фильтры, карточки рисков, переход к фрагменту), экспорт, администрирование (пользователи, провайдеры, модели, настройки, аудит).
- Без фиктивных данных: пустые состояния с подсказками.
- CSP: `default-src 'self'; script-src 'self'; style-src 'self'` (Tailwind собирается в файл, inline-стилей и скриптов нет), `frame-ancestors 'none'`.
- Тесты: Vitest + Testing Library + MSW.

## 17. Экспорт

- PDF — reportlab со встроенным шрифтом с кириллицей (DejaVu/Noto, свободная лицензия), весь пользовательский/модельный текст экранируется перед `Paragraph`. DOCX — python-docx. CSV — UTF-8 с BOM, `;`-разделитель, XLSX — openpyxl.
- Formula injection: значения, начинающиеся с `=`, `+`, `-`, `@`, `\t`, `\r`, префиксуются `'`; в XLSX значения записываются как строки (`data_type='s'`).
- Содержимое: название документа, версия, дата, сторона, snapshot модели (без секретов), категории, сводка, риски с цитатами, страницами (с источником страниц), рекомендации, статусы проверки, ограничения и правовая оговорка.
- Генерация — задачей worker'а; скачивание — `GET /exports/{id}/download` с проверкой прав через анализ → документ; `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`.

## 18. Тестовая стратегия

| Уровень | Инструменты | Что покрывает |
|---|---|---|
| Unit (backend) | pytest | нормализация и сопоставление цитат, чанкинг, валидаторы файлов, SSRF-валидатор, политики RBAC, редактирование логов, formula injection, схемы ответа |
| Integration (backend) | pytest + реальный PostgreSQL (контейнер или локальный), httpx `ASGITransport`, `respx` для провайдеров | API-эндпоинты, миграции на пустой БД, IDOR-матрица, bootstrap, очередь задач (lease/reaper), конвейер анализа с mock-провайдером, повтор/отмена/идемпотентность |
| Документы | файлы-фикстуры, генерируемые в тестах (python-docx, PyMuPDF): таблицы, пустые, повреждённые, подмена MIME, zip-bomb, PDF без текста; DOC — только если доступен converter (иначе тест помечается skip с причиной) | |
| Frontend | Vitest, Testing Library, MSW | вход, загрузка, выбор модели, облачное согласие, фильтры, статусы доказательств, ошибки, ролевые экраны |
| E2E | Playwright против compose-стека с mock-провайдером (этап 08) | bootstrap → login → upload → extraction → analysis → evidence → export |
| Качество LLM | небольшой размеченный набор синтетических договоров (без реальных данных), ручной прогон с реальной моделью | точность цитат, доля rejected; не входит в CI |
| Статический анализ | ruff, mypy (strict для всего backend), oxlint (в т.ч. `react/no-danger`), `tsc --noEmit`, `pip-audit`/`npm audit` | |

Live-проверки OpenRouter/Ollama не входят в CI; если не выполнялись — это указывается в статусе.

## 19. Резервное копирование и восстановление

- Данные: PostgreSQL (`pgdata`) + файловое хранилище (`storage`). Секреты (`.env`, `SECRETS_ENCRYPTION_KEY`) копируются отдельно в secret manager — без ключа шифрования секреты провайдеров в бэкапе не восстановить (это намеренно).
- Бэкап: `pg_dump -Fc` из контейнера `db` → затем архив `storage` (порядок: сначала БД, затем файлы; файлы, на которые нет ссылок, допустимы, обратное — нет). Скрипты `deploy/backup.sh`, `deploy/restore.sh`. Бэкапы шифруются (например, `age`/`gpg`) и хранятся вне хоста.
- Восстановление: остановить backend/worker → `pg_restore --clean --if-exists` в пустую БД → распаковать storage → `alembic upgrade head` → запустить → проверить `/api/health/ready`. Процедура проверяется на этапе 08/09 на чистом стенде.

## 20. Production-развёртывание

- Внешний TLS reverse proxy (Caddy/Traefik/nginx) → `frontend`; HSTS; только HTTPS; `Secure` cookies (`COOKIE_SECURE=true` обязателен при `ENV=production`, проверяется при старте).
- `ENV=production`: debug выключен, OpenAPI скрыт, mock-провайдер недоступен, `SECRET_KEY`/`SECRETS_ENCRYPTION_KEY`/`DATABASE_URL`/`ALLOWED_ORIGINS` обязательны, иначе старт завершается ошибкой.
- Контейнеры без root, `read_only` где возможно, `cap_drop: ALL`, `no-new-privileges`, лимиты ресурсов, healthchecks, `restart: unless-stopped`.
- PostgreSQL не публикуется наружу; отдельные роли БД: миграции (DDL) и приложение (DML, без UPDATE/DELETE на `audit_log`).
- Обновление: бэкап → pull/build → `migrate` → rolling restart backend/worker. Миграции, удаляющие данные, выполняются только после явного согласования и с бэкапом.
- Ротация секретов: `SECRETS_ENCRYPTION_KEY` поддерживает список ключей (MultiFernet) и команду перешифровки; ключи провайдеров меняются через админку; `SECRET_KEY` ротация инвалидирует сессии (документируется).

## 21. Известные ограничения

- PDF без текстового слоя не поддерживается (нет OCR).
- Страницы DOCX/DOC приблизительны (разметка Word/LibreOffice), отмечаются источником.
- Автонумерация пунктов в DOCX определяется только для типовых форматов.
- Смысловая проверка цитат выполняется моделью-верификатором и не является гарантией; итог требует проверки специалистом.
- Нормы права, на которые ссылается модель, сервером не проверяются.
- Облачный провайдер получает переданный текст; условия хранения определяются провайдером.
- PyMuPDF распространяется под AGPL-3.0: текущая модель — только внутреннее использование; при доступе внешних пользователей или поставке третьим лицам нужна коммерческая лицензия или замена (pypdfium2) — см. [LICENSES.md](LICENSES.md).
