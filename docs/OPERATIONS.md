# Эксплуатация

Документ дополняется на каждом этапе. Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Локальный запуск через Docker Compose

Требования: Docker Engine/Desktop с Compose v2, ~4 ГБ свободной памяти (образ converter с LibreOffice ~600 МБ).

```bash
cp .env.example .env
# Заполните POSTGRES_PASSWORD, SECRET_KEY, SECRETS_ENCRYPTION_KEYS (команды генерации — в .env.example)
# и BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD / BOOTSTRAP_ADMIN_NAME для первого Superadmin (§9)
docker compose up -d --build
docker compose ps                      # все сервисы healthy, migrate — exited (0)
curl -s http://127.0.0.1:8080/api/health/ready
```

Приложение: http://localhost:8080. При `APP_ENV=development` OpenAPI доступен на `/api/docs`. Войдите под e-mail и паролем из `BOOTSTRAP_ADMIN_*` — система сразу попросит сменить пароль.

Порядок старта: `db` (healthy) → `migrate` (`alembic upgrade head`, завершается) → `backend` и `worker` → `frontend`.

| Сервис | Назначение | Наружу |
|---|---|---|
| `frontend` | nginx: SPA + прокси `/api` | `127.0.0.1:8080` (`HTTP_BIND`, `HTTP_PORT`) |
| `backend` | FastAPI | нет |
| `worker` | фоновые задачи | нет |
| `converter` | LibreOffice, сеть без выхода в интернет | нет |
| `db` | PostgreSQL 17 | нет (см. `compose.dev.yaml`) |
| `ollama` | опционально, `--profile ollama` | нет |

Остановка: `docker compose down` (данные в volumes `pgdata`, `storage` сохраняются). `docker compose down -v` **удаляет все данные** — не используйте без бэкапа.

## 2. Разработка без Docker для backend/frontend

PostgreSQL для разработки — из compose, опубликованный только на localhost:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d db     # 127.0.0.1:55432
```

Backend (Python 3.12+):

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
export ENV=development DATABASE_URL=postgresql+psycopg://cai:<пароль>@127.0.0.1:55432/cai \
       SECRET_KEY=<...> SECRETS_ENCRYPTION_KEYS=<...> ALLOWED_ORIGINS=http://localhost:5173 \
       COOKIE_SECURE=false STORAGE_DIR=./.data/storage
alembic upgrade head
python -m app.server          # API на :8000
python -m app.worker          # worker
```

Frontend:

```bash
cd frontend
npm ci
npm run dev                   # http://localhost:5173, /api проксируется на 127.0.0.1:8000
```

## 3. Проверки

```bash
# backend
cd backend
ruff check app tests && ruff format --check app tests
mypy app tests
TEST_DATABASE_URL=postgresql+psycopg://<user>:<pw>@127.0.0.1:55432/<db> pytest
# без TEST_DATABASE_URL интеграционные тесты пропускаются (skip) с пояснением

# frontend
cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

Интеграционные тесты создают отдельную временную БД (`cai_test_*`) и удаляют её после прогона; пользователю из `TEST_DATABASE_URL` нужно право `CREATEDB`.

## 4. Миграции

- Миграции применяет только одноразовый сервис `migrate` (`alembic upgrade head`). Backend сам миграции не выполняет; пока схема не на `head`, `/api/health/ready` возвращает 503 (`migrations: pending`).
- Вся миграция выполняется в одной транзакции; параллельный запуск блокируется advisory lock; `lock_timeout = 10s`.
- `alembic downgrade` при `ENV=production` запрещён, пока не задан `ALLOW_DOWNGRADE=1` (только после бэкапа).
- Тест `tests/unit/test_migration_policy.py` не пропускает в `upgrade()` операции `drop_*`, `alter_column`, `execute` без комментария `# destructive-approved: <ссылка на согласование>`.
- Тест `tests/integration/test_database.py` проверяет, что модели совпадают со схемой после миграций и что upgrade → downgrade → upgrade проходит на чистой БД.
- Новая миграция: `alembic revision --autogenerate -m "..."` → ручная проверка файла → тесты.
- Обновление в production: бэкап → `docker compose build` → `docker compose run --rm migrate` → `docker compose up -d`.

## 5. Резервное копирование и восстановление

Данные: PostgreSQL (volume `pgdata`) и файловое хранилище (volume `storage`: оригиналы, извлечённый текст, экспорты). Секреты (`.env`) в бэкап не входят — храните их отдельно в secret manager. Без `SECRETS_ENCRYPTION_KEYS` сохранённые ключи провайдеров из бэкапа не расшифровать (это намеренно).

### Бэкап

```bash
deploy/backup.sh ./backups        # → ./backups/<UTC-время>/{db.dump, storage.tar.gz, SHA256SUMS}
```

- `pg_dump -Fc` выполняется внутри контейнера `db`, приложение можно не останавливать (снимок БД консистентен).
- Сначала дампится БД, затем архивируется storage: файлы, на которые нет ссылок, допустимы, обратное — нет.
- Бэкап содержит конфиденциальные договоры: шифруйте его (например, `age -r <ключ> -o backup.tar.age`) и храните вне хоста. Удалённые в приложении документы остаются в бэкапах до их ротации.

Эквивалент вручную:

```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > db.dump
docker run --rm -v contract-ai-analyzer_storage:/data:ro -v "$PWD":/backup alpine:3 tar -C /data -czf /backup/storage.tar.gz .
```

### Восстановление

```bash
deploy/restore.sh ./backups/<UTC-время> --confirm
```

Скрипт: проверяет контрольные суммы → останавливает `frontend`, `backend`, `worker` → `pg_restore --clean --if-exists --single-transaction` → заменяет содержимое volume `storage` → `migrate` → запуск. **Операция заменяет текущие данные**, поэтому без `--confirm` скрипт не выполняется.

После восстановления проверьте `curl -s http://127.0.0.1:8080/api/health/ready` и выборочно открытие документов.

## 6. Логи

- Все сервисы пишут JSON в stdout; docker хранит до 5×10 МБ на контейнер (`docker compose logs -f backend worker`).
- В логах нет паролей, токенов, ключей API, URL подключения к БД с паролем и текста договоров: значения редактируются (`app/logging.py`), access-лог пишет только метод, путь без query-строки, статус и длительность.
- Каждому запросу присваивается `request_id` (заголовок `X-Request-ID`, поле в логах и в ответах с ошибкой).

## 7. Production

- Запускать только за внешним TLS reverse proxy (Caddy/Traefik/nginx), который проксирует на `frontend:8080`; `HTTP_BIND=127.0.0.1`.
- `.env`: `APP_ENV=production`, `COOKIE_SECURE=true`, `ALLOWED_ORIGINS=https://<домен>`. Backend откажется стартовать с небезопасной конфигурацией (http-origin, `COOKIE_SECURE=false`, пустые ключи, ключ-заглушка, `LOG_LEVEL=DEBUG`).
- При `ENV=production`: OpenAPI выключен, ответы содержат HSTS.
- Контейнеры `backend`, `worker`, `converter`, `frontend` работают без root, с read-only FS, `cap_drop: ALL`, `no-new-privileges`. PostgreSQL не публикуется наружу; сети `data` и `convert` без выхода в интернет.
- Образ `ollama/ollama:latest` в профиле `ollama` не зафиксирован по версии — для production укажите конкретный тег.
- **Лицензия PyMuPDF (AGPL-3.0):** развёртывание допустимо для внутренних пользователей организации. Не открывайте доступ внешним пользователям и не передавайте образы третьим лицам, пока не решён вопрос лицензии (коммерческая лицензия Artifex, открытие кода или замена на pypdfium2) — см. [LICENSES.md](LICENSES.md).

## 8. Зависимости

- Backend-образ ставит пакеты только из `backend/requirements.lock` с проверкой хешей. Пересоздать lock после изменения `pyproject.toml` (в Linux-контейнере, чтобы колёса соответствовали образу):

  ```bash
  cd backend
  docker run --rm -v "$PWD/pyproject.toml:/src/pyproject.toml:ro" -v "$PWD:/out" -w /src python:3.13-slim-trixie \
    sh -c "pip install -q uv && uv pip compile --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
           --generate-hashes --no-header pyproject.toml -o /out/requirements.lock"
  ```
- Frontend-образ использует `npm ci` по `package-lock.json`.

## 9. Учётные записи и доступ

### Первый Superadmin (bootstrap)

Пароля по умолчанию нет. Первый Superadmin создаётся из переменных окружения сервиса `backend`:

```bash
# .env
BOOTSTRAP_ENABLED=true
BOOTSTRAP_ADMIN_EMAIL=admin@company.example
BOOTSTRAP_ADMIN_PASSWORD='<не короче 12 символов; не из списка распространённых>'
BOOTSTRAP_ADMIN_NAME=Иван Петров
docker compose up -d backend
docker compose logs backend | grep bootstrap      # outcome: created
```

- Создание выполняется при старте backend в одной транзакции под advisory lock и только если Superadmin ещё нет и bootstrap не выполнялся ранее. Повторный старт ничего не меняет (`outcome: skipped_superadmin_exists`) — ни пароль, ни роль.
- При первом входе система потребует сменить пароль.
- **После первого входа** удалите `BOOTSTRAP_ADMIN_PASSWORD` (и остальные `BOOTSTRAP_ADMIN_*`) из `.env`, установите `BOOTSTRAP_ENABLED=false` и перезапустите backend. Пока пароль остаётся в окружении, при каждом старте в логе будет предупреждение `bootstrap_password_still_configured`.
- Если переменные не заданы или пароль не проходит политику: аккаунт не создаётся, в логе — `bootstrap_failed` с именами переменных (без значений), `/api/health/ready` → 503 `accounts: bootstrap_required`, страница входа показывает предупреждение.
- Ручной запуск (идемпотентно): `docker compose exec backend python -m app.cli bootstrap` — код выхода 0 (создан или уже существует), 1 (администратора нет и создать не удалось), 2 (ошибка конфигурации).
- Если пользователь с `BOOTSTRAP_ADMIN_EMAIL` уже существует, bootstrap его не повышает.

### Пароли, сессии, блокировки

| Параметр | По умолчанию | Назначение |
|---|---|---|
| `SESSION_IDLE_MINUTES` | 480 | Сессия завершается после бездействия |
| `SESSION_ABSOLUTE_HOURS` | 168 | Максимальный срок сессии |
| `LOGIN_WINDOW_MINUTES` | 15 | Окно подсчёта неудачных попыток входа |
| `LOGIN_MAX_FAILURES_PER_EMAIL` | 5 | Неудач на один e-mail в окне, затем 429 |
| `LOGIN_MAX_FAILURES_PER_IP` | 30 | Неудач с одного IP в окне, затем 429 |
| `LOGIN_DELAY_STEP_MS` / `LOGIN_DELAY_MAX_MS` | 500 / 3000 | Прогрессивная задержка ответа после неудач |

- Пароли — Argon2id (t=3, m=64 МиБ, p=4), не короче 12 и не длиннее 128 символов, не из списка распространённых, без e-mail/имени. При изменении параметров хеш пересчитывается при следующем входе.
- IP клиента берётся из `X-Forwarded-For` только от доверенного прокси (`TRUSTED_PROXY_IPS`); за внешним reverse proxy убедитесь, что nginx `frontend` получает реальный адрес клиента, иначе лимит по IP будет общим для всех.
- `ALLOWED_ORIGINS` должен содержать **точный** адрес, по которому открывают приложение в браузере (схема, хост, порт). `http://localhost:8080` и `http://127.0.0.1:8080` — разные origin. Иначе вход и любые изменения отклоняются с `csrf_failed`; в логе backend — `csrf_rejected` с полученным `origin` и причиной (`origin_not_allowed` / `token_mismatch`). После изменения `.env` перезапустите backend: `docker compose up -d backend`.
- Ротация `SECRET_KEY` завершает все сессии (в БД хранится HMAC токенов) и сбрасывает счётчики неудачных входов.
- Записи `login_attempts` старше суток удаляются автоматически при следующих попытках входа.

### Управление пользователями (UI «Пользователи», API `/api/users`)

- Admin создаёт и изменяет Admin/Analyst/Viewer; Superadmin — всех. Admin не может назначить роль Superadmin и изменить/заблокировать Superadmin. Никто не может изменить свою роль или заблокировать себя. Последнего активного Superadmin понизить или заблокировать нельзя.
- Создание пользователя и сброс пароля выдают **одноразовый временный пароль** — он показывается один раз, передайте его пользователю защищённым каналом. Сброс завершает все сессии пользователя и снимает блокировку входа.
- Блокировка завершает все сессии; изменение роли действует со следующего запроса.
- «Доступ к моделям»: персональное разрешение/запрет поверх ролей модели. Выключенная модель недоступна никому.

### Аудит

Журнал (UI «Журнал аудита», API `/api/admin/audit`, Admin и Superadmin) — append-only таблица `audit_log`: входы и неудачные входы (с IP и причиной, без введённых данных), выход и завершение сессий, смена/сброс пароля, создание пользователей, изменение ролей, блокировка, доступ к моделям, обращение Superadmin к чужим документам; с этапа 04 — загрузка (`document.uploaded`: ID версии, формат, размер, SHA-256), скачивание оригинала, повтор извлечения, выдача/отзыв доступа, удаление документа (`document.deleted`: SHA-256 оригиналов и владелец). С этапа 05 — провайдеры и модели (`provider.created/updated/deleted/checked`, `model.created/updated/deleted/tested/imported` с изменёнными полями, ключ — только как `***`), изменение настроек облака (`settings.updated`) и подтверждение передачи во внешний сервис (`llm.cloud_consent_recorded`). Действия с анализами и экспортом добавляются на этапах 06–07. Пароли, токены, ключи, тексты, названия договоров и имена файлов в журнал не пишутся.

## 10. Документы: загрузка и извлечение текста

### Параметры

| Параметр | По умолчанию | Назначение |
|---|---|---|
| `MAX_UPLOAD_MB` | 25 | Максимальный размер файла. Должен быть меньше `client_max_body_size` в `frontend/nginx.conf` (30m) |
| `MAX_PAGES` | 500 | Максимум страниц (PDF — по файлу; DOCX — по `docProps/app.xml` и рендеру) |
| `MAX_PENDING_EXTRACTIONS_PER_USER` | 10 | Сколько документов пользователя может одновременно ждать извлечения (дальше — 429) |
| `EXTRACTION_TIMEOUT_SECONDS` | 180 | Таймаут дочернего процесса разбора (процесс убивается) |
| `EXTRACTION_MEMORY_MB` | 1024 | `RLIMIT_AS` дочернего процесса разбора |
| `LIBREOFFICE_TIMEOUT_SECONDS` | 90 | Таймаут LibreOffice в контейнере `converter` (группа процессов убивается) |
| `CONVERTER_TIMEOUT_SECONDS` | 180 | Таймаут ожидания ответа converter на стороне worker'а; должен быть больше очереди converter (60 с) + `LIBREOFFICE_TIMEOUT_SECONDS` |

### Как это работает

1. `POST /api/documents` (тело — файл, имя — `X-File-Name`, `Idempotency-Key` — для безопасного повтора) — поток пишется во временный файл с лимитом размера и SHA-256; формат определяется по сигнатуре и структуре (PDF `%PDF-`, DOCX — ZIP с `word/document.xml` без макросов, DOC — OLE с потоком `WordDocument`), расширение и `Content-Type` должны совпадать с форматом. Оригинал хранится в `storage/originals/<uuid>` без имени и расширения; выдаётся только через `GET /api/documents/{id}/versions/{vid}/file` (`attachment`, `nosniff`).
2. Задача `extract_document` в worker: DOC → DOCX через `converter`; разбор PDF/DOCX — в дочернем процессе без секретов в окружении, с таймаутом и лимитами памяти/CPU; для DOCX без сохранённой разметки Word страницы определяются рендером LibreOffice (DOCX → PDF). Результат — канонический текст (`storage/text/<uuid>`), фрагменты и страницы в БД.
3. Статусы версии: `pending` → `processing` → `ready` | `unsupported_no_text` (скан без текста, OCR нет) | `failed` (код в `extraction_error_code`). Недоступность converter — временная ошибка (повтор задачи); после исчерпания попыток DOC получает `failed`, а DOCX — `ready` без номеров страниц (предупреждение `pages_unavailable`). Неудачное извлечение можно повторить: кнопка в карточке документа или `POST .../retry-extraction`.
4. Удаление (`DELETE /api/documents/{id}`): документ сразу скрывается (`deleting`), задача `purge_document` удаляет файлы (оригинал, DOCX после конвертации, канонический текст, экспорты), затем строки. В резервных копиях данные остаются до их ротации.

### Диагностика

- Документ долго в `pending`: проверьте worker (`docker compose ps worker`, `docker compose logs worker | grep extraction`) и очередь (`SELECT type, status, attempts, last_error_code FROM jobs ORDER BY created_at DESC LIMIT 20;`).
- `converter_unavailable` / `converter_busy`: `docker compose ps converter`, `docker compose logs converter` (в логе — только направление, размер, длительность и результат конвертации).
- `extraction_timeout` / `resource_limit_exceeded`: файл слишком сложный для лимитов; при необходимости увеличьте `EXTRACTION_TIMEOUT_SECONDS` / `EXTRACTION_MEMORY_MB` (и `mem_limit` worker'а в `compose.yaml`).
- Тексты документов и имена файлов в логи не пишутся; ошибки разбора логируются только кодами.


## 11. Модели и провайдеры (OpenRouter, Ollama)

Раздел UI «Модели» (`/admin/models`) доступен Admin и Superadmin. Провайдеры (адрес, ключ, частная сеть, включение, удаление) и глобальный выключатель облачных моделей меняет только Superadmin; модели (добавление, импорт, включение, роли, параметры, проверка) — Admin и Superadmin. Изменения применяются сразу, без пересборки и перезапуска; каждое изменение пишется в историю (`config_revisions`) и аудит.

### Облачные модели и предупреждение

- По умолчанию облачные модели **выключены** (`system_settings.cloud_models_enabled` отсутствует = false). Пока они выключены, пользователи не видят моделей облачных провайдеров, а сервер отклоняет их выбор (`cloud_models_disabled`).
- Текст предупреждения о передаче фрагментов договора внешнему провайдеру редактируется там же; при изменении текста увеличивается его версия, и пользователи подтверждают его заново. Текст не должен утверждать, что провайдер не хранит данные.
- Аварийное отключение облака: кнопка «Отключить облачные модели» (или отключить провайдера OpenRouter). Новые облачные запуски отклоняются; уже запущенные облачные анализы останавливаются перед ближайшим вызовом модели со статусом `failed` (`cloud_models_disabled` / `model_disabled`) — полученные результаты сохраняются, после повторного включения анализ можно повторить (§12).

### OpenRouter

1. Superadmin: «Добавить провайдера» → тип OpenRouter, адрес по умолчанию `https://openrouter.ai/api/v1`, API-ключ. Ключ шифруется (`SECRETS_ENCRYPTION_KEYS`) и больше не показывается — только «ключ задан (…последние 4 символа)».
2. «Проверить соединение» — запрос `GET /key` без генерации (бесплатно). `provider_auth_failed` — ключ неверный или отозван.
3. «Импорт моделей» — список из `GET /models` с контекстом и ценами (USD за 1 млн токенов, источник «из API провайдера» с датой). Импортированные модели выключены.
4. Карточка модели → «Проверить с генерацией» — короткий тестовый запрос без данных договоров (**тарифицируется**); только после него режим «JSON Schema» считается подтверждённым. Затем включите модель, выберите роли и назначение.
- Смена адреса провайдера без ввода нового ключа сбрасывает сохранённый ключ (чтобы ключ не ушёл на новый адрес).

### Ollama

Модели **не скачиваются автоматически** — администратор скачивает их сам (`ollama pull <модель>`).

| Где запущена Ollama | Адрес в провайдере | «Разрешить частную сеть» |
|---|---|---|
| В Compose: `docker compose --profile ollama up -d`, затем `docker compose exec ollama ollama pull qwen3:8b` | `http://ollama:11434` | да |
| На хосте Docker | `http://host.docker.internal:11434` (Docker Desktop) или IP хоста в частной сети; на Linux добавьте `extra_hosts: ["host.docker.internal:host-gateway"]` сервисам backend и worker и запустите Ollama с `OLLAMA_HOST=0.0.0.0` | да |
| На отдельном сервере в локальной сети | `http://10.x.x.x:11434` (или `https://`, если за TLS-прокси) | да |
| Во внешней сети (интернет) | только `https://…`; провайдер считается **облачным** (подпадает под выключатель облака и предупреждение) | нет |

- `localhost`/`127.0.0.1` внутри контейнера — это сам контейнер backend, а не хост; UI показывает предупреждение.
- Проверено (2026-10-17): Docker Desktop на Windows, Ollama 0.40.4 на хосте — адрес `http://host.docker.internal:11434` (разрешается в 192.168.65.254, нужна частная сеть), отдельная настройка `OLLAMA_HOST` не потребовалась.
- Без GPU генерация медленная (минуты на запрос для модели 8B): задайте «Таймаут» провайдера с запасом (300–900 с). Модели с рассуждением (deepseek-r1 и др.) тратят лимит ответа и на рассуждение — «Максимум токенов ответа» задавайте с запасом (тестовая генерация использует лимит модели, не больше 2048).
- «Разрешить частную сеть» включает адреса 10/8, 172.16/12, 192.168/16, loopback, CGNAT, ULA и `http`. Metadata-адреса облаков (169.254.169.254 и др.) и link-local запрещены всегда.
- При включённой частной сети провайдер по умолчанию **локальный**: запросы к публичным адресам блокируются (`public_address_blocked`), даже если DNS-имя изменится. Флаг «Передача данных за пределы организации» делает его облачным.
- Если Ollama за reverse proxy с авторизацией, ключ задаётся в поле API-ключа (передаётся как `Authorization: Bearer`). Дополнительные заголовки провайдера не могут содержать секреты.

### Ротация ключа шифрования секретов

1. Сгенерируйте новый ключ: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
2. Поставьте его **первым** в `SECRETS_ENCRYPTION_KEYS` (старый — через запятую после него), перезапустите backend и worker: `docker compose up -d backend worker`.
3. Перешифруйте ключи провайдеров: `docker compose exec backend python -m app.cli rotate-secrets` (выводит число перешифрованных; код 1 — часть ключей не расшифровалась, их нужно ввести заново в UI).
4. Удалите старый ключ из `SECRETS_ENCRYPTION_KEYS` и снова перезапустите backend и worker.

Потеря ключа шифрования не затрагивает остальные данные: ключи провайдеров нужно будет ввести заново (`secret_undecryptable`).

### Диагностика

| Код | Причина и действие |
|---|---|
| `provider_auth_failed` | неверный/отозванный ключ — введите новый |
| `provider_quota_exceeded` | недостаточно средств или исчерпан лимит у провайдера |
| `provider_rate_limited` | 429; повторы выполняются автоматически (до 3, с учётом `Retry-After` ≤ 60 с) |
| `model_unavailable` | модели нет у провайдера / не скачана в Ollama (`ollama pull`) |
| `dns_failed`, `connection_failed` | адрес недоступен из контейнера (проверьте сеть, имя сервиса, профиль `ollama`) |
| `https_required`, `private_address_blocked`, `loopback_blocked`, `metadata_blocked`, `public_address_blocked`, `unexpected_redirect` | адрес запрещён политикой SSRF — см. таблицу выше |
| `api_key_missing`, `secret_undecryptable` | ключ не задан или не расшифровывается текущими `SECRETS_ENCRYPTION_KEYS` |

В логах — только метаданные вызовов (`llm_http`: провайдер, путь, статус, длительность; `llm_retry`: код ошибки, попытка, задержка). Ключи, тексты запросов и ответов провайдеров не логируются.

## 12. Анализ договоров

Анализ выполняет worker (задача `run_analysis`); API только проверяет запрос и ставит задачу. Форма запуска и отчёт в UI — этап 07; до этого анализ запускается через API (ниже). Результат не заменяет консультацию юриста — это указано в каждой сводке.

### Параметры

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ANALYSIS_MAX_TOKENS` | 1 500 000 | предел токенов (вход + выход) на анализ: при запуске — по оценке (422 `analysis_budget_exceeded`), во время работы — по расходу, который сообщил провайдер (оставшиеся части пропускаются, статус `completed_with_warnings`) |
| `ANALYSIS_MAX_ACTIVE_PER_USER` | 3 | одновременно выполняющихся анализов одного пользователя (429 `too_many_active_analyses`) |
| `ANALYSIS_DEFAULT_MAX_OUTPUT_TOKENS` | 8192 | лимит ответа модели, если в каталоге не задан «Максимум токенов ответа» |
| `ANALYSIS_QUOTE_RETRY_LIMIT` | 30 | сколько раз за анализ можно переспросить модель о точной цитате |
| `ANALYSIS_CANCEL_POLL_SECONDS` | 2 | как часто во время вызова модели проверяется запрос отмены (в compose не вынесен) |

Размер части текста (чанка) зависит от модели: контекст из каталога (если неизвестен — 8192) минус 2500 токенов на инструкции минус лимит ответа, но не больше 12 000 / 8 000 / 4 000 токенов для глубины «быстрый / стандартный / углублённый». Если модель не вмещает даже 1000 токенов текста — 422 `model_context_too_small` (уменьшите лимит ответа или выберите модель с большим контекстом).

### Запуск через API

```bash
# Сессия и CSRF — как в UI: cookie cai_session + заголовок X-CSRF-Token (значение cookie cai_csrf)
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -H "Origin: http://127.0.0.1:8080" \
  -H "Content-Type: application/json" -H "Idempotency-Key: $(uuidgen)" \
  -d '{"document_id":"…","party":"party_a","categories":["payment","liability","termination","contradiction","missing_clause"],
       "depth":"standard","model_id":"…","cloud_consent":false}' \
  http://127.0.0.1:8080/api/analyses
```

- `party`: `party_a` — первая сторона, названная в преамбуле, `party_b` — вторая, `named` + `party_name`, `unknown` (риски описываются нейтрально, без оценки выгодности).
- `categories`: `payment`, `liability`, `penalties`, `deadlines`, `renewal`, `termination`, `unilateral_changes`, `warranties`, `confidentiality`, `personal_data`, `intellectual_property`, `governing_law`, `force_majeure`, `acceptance`, `undefined_terms`, `contradiction`, `missing_clause`, `other`. Межраздельный проход выполняется, только если выбраны `contradiction` или `missing_clause`.
- `depth`: `quick` | `standard` | `deep`; `verifier_model_id` — другая модель для смысловой проверки цитат (по умолчанию та же).
- Облачная модель: облачные модели включены Superadmin'ом, пользователь принял текущую версию предупреждения (`POST /api/models/{id}/cloud-consent`, иначе 409 `cloud_notice_not_accepted`) **и** передал `"cloud_consent": true` в этом запросе (иначе 409 `cloud_consent_required`).
- `POST /api/analyses/estimate` (то же тело) — оценка токенов и стоимости (стоимость — только если цена модели известна) без запуска и без передачи данных.
- Прогресс: `GET /api/analyses/{id}` (статус, этап, части, сводка, покрытие, snapshot, расход); риски: `GET /api/analyses/{id}/risks` (`?validation_status=&severity=&kind=&category=&include_rejected=true`); список: `GET /api/analyses?document_id=`.
- Отмена: `POST /api/analyses/{id}/cancel`; повтор неудачного или отменённого: `POST /api/analyses/{id}/retry` (`{"cloud_consent": true}` для облачного) — готовые части не пересчитываются.

### Статусы и проверка

- Анализ: `queued` → `running` (структура, части) → `verifying` (проверка цитат) → `completed` | `completed_with_warnings` (часть текста не проанализирована, верификатор недоступен, межраздельный проход не выполнен и т.п. — см. `coverage` и `summary.warnings`) | `failed` (`error_code`, безопасное `error_message`) | `cancelling` → `cancelled`.
- Риск: `verified_match` — цитата найдена в тексте и, по оценке верификатора, подтверждает риск; `needs_review` — требует проверки (частичная поддержка, верификатор недоступен, отсутствующее условие, противоречие без двух подтверждённых цитат); `rejected` — цитата не найдена или выдумана, фрагмент не существует или не был в запросе, верификатор отверг связь, модель отозвала риск. Отклонённые скрыты по умолчанию и не являются установленным фактом.
- Страницы и пункты в доказательствах — из метаданных документа (для DOCX/DOC — приблизительно, `pages_approximate`).

### Восстановление и повторы

- Результат каждой части и этапа сохраняется сразу; при падении worker'а задача возвращается в очередь после истечения lease (`JOB_LEASE_SECONDS`) и продолжает с незавершённой части.
- Временные ошибки провайдера (таймаут, 429, 5xx, сеть) повторяются внутри запроса (до 3 раз) и затем повтором задачи (до 4 попыток с паузой); на последней попытке анализ получает `failed` с кодом ошибки. Неустранимые (ключ, баланс, модель недоступна, модель/провайдер отключены, облако выключено) — сразу `failed`; после исправления — «Повторить».
- Анализ, у которого не осталось активной задачи (задача исчерпала попытки), worker закрывает со статусом `failed` (`job_lost`) — его можно повторить.
- Медленная локальная модель (Ollama на CPU отвечает минутами): задайте «Таймаут» провайдера 300–900 с и лимит ответа с запасом для моделей с рассуждением; используйте глубину «быстрый» (меньше вызовов). Для Ollama сервер сам задаёт `num_ctx` по размеру запроса (не больше контекста модели).

### Диагностика

| Код | Причина и действие |
|---|---|
| `analysis_no_chunks_succeeded` | модель ни разу не вернула ответ по схеме — проверьте модель «Проверить с генерацией», режим structured output, лимит ответа |
| `invalid_model_output` (у части) | ответ не прошёл схему и после одной попытки исправления |
| `output_truncated` (у части) | ответ обрезан по лимиту — увеличьте «Максимум токенов ответа» модели |
| `context_too_long` (у части) | запрос не поместился в контекст — уменьшите глубину/лимит ответа или укажите контекст модели в каталоге |
| `token_budget_exhausted` (у части) | исчерпан `ANALYSIS_MAX_TOKENS` во время работы |
| `cloud_models_disabled`, `model_disabled`, `model_not_found` | облако или модель отключены/удалены после запуска |
| `cloud_consent_missing` | провайдер стал облачным после запуска (передача не подтверждалась) — запустите новый анализ с подтверждением |
| `job_lost` | задача исчерпала попытки (падения worker'а) — «Повторить» |

В логах worker'а — только метаданные: `analysis_started_run`, `analysis_chunk_done` / `analysis_chunk_failed` (номер части, число рисков, код), `analysis_quote_retry`, `analysis_risks_materialized`, `analysis_completed` (число рисков, предупреждений, расход), `analysis_prompt_injection_suspected` (число совпадений), `analysis_retry_scheduled`, `analysis_failed` (код). Текст договора, цитаты и ответы модели не логируются. Аудит: `analysis.started` (документ, версия, модель, облако, сторона-код, глубина, число категорий и частей, оценка токенов), `analysis.cancelled`, `analysis.retried`; обращение Superadmin к чужому анализу — `document.accessed_by_superadmin`.
