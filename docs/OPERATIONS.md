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

На Windows порт может оказаться в диапазоне, зарезервированном Hyper-V/WSL (`bind: An attempt was made to access a socket in a way forbidden by its access permissions`). Диапазоны: `netsh interface ipv4 show excludedportrange protocol=tcp`; задайте другой порт через `DEV_DB_PORT` (например, `DEV_DB_PORT=56432`) и используйте его в `DATABASE_URL`/`TEST_DATABASE_URL`. Так было на этапе 09: 55432 попал в зарезервированный диапазон 55333–55432.

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

# зависимости: уязвимости (pip-audit — во временном venv, чтобы не менять dev-окружение)
python -m venv /tmp/audit && /tmp/audit/bin/pip install pip-audit pip-licenses
/tmp/audit/bin/pip-audit -r backend/requirements.lock --no-deps --disable-pip
(cd frontend && npm audit)

# end-to-end на отдельном чистом стенде Docker (§15)
e2e/run.sh all
```

Интеграционные тесты создают отдельную временную БД (`cai_test_*`) и удаляют её после прогона; пользователю из `TEST_DATABASE_URL` нужно право `CREATEDB`. Тест `tests/integration/test_security_sweep.py` перебирает все маршруты API из OpenAPI-схемы (401/CSRF/RBAC/IDOR); при добавлении маршрута без ID объекта в пути внесите его в `SELF_ROUTES` или `PUBLIC_ROUTES` — иначе тест упадёт.

## 4. Миграции

- Миграции применяет только одноразовый сервис `migrate` (`alembic upgrade head`). Backend сам миграции не выполняет; пока схема не на `head`, `/api/health/ready` возвращает 503 (`migrations: pending`).
- Вся миграция выполняется в одной транзакции; параллельный запуск блокируется advisory lock; `lock_timeout = 10s`.
- `alembic downgrade` при `ENV=production` запрещён, пока не задан `ALLOW_DOWNGRADE=1` (только после бэкапа).
- Тест `tests/unit/test_migration_policy.py` не пропускает в `upgrade()` операции `drop_*`, `alter_column`, `execute` без комментария `# destructive-approved: <ссылка на согласование>`. Исключение — `op.execute` с пометкой `# non-destructive-sql: <пояснение>`: тест дополнительно проверяет, что SQL в `upgrade()` не содержит `DROP`, `DELETE FROM`, `TRUNCATE`, `UPDATE … SET`, `ALTER TABLE/TYPE` (так создаются, например, триггеры).
- Тест `tests/integration/test_database.py` проверяет, что модели совпадают со схемой после миграций и что upgrade → downgrade → upgrade проходит на чистой БД.
- Новая миграция: `alembic revision --autogenerate -m "..."` → ручная проверка файла → тесты.
- Обновление в production — порядок в §14.4.

## 5. Резервное копирование и восстановление

Данные: PostgreSQL (volume `pgdata`) и файловое хранилище (volume `storage`: оригиналы, извлечённый текст, экспорты). Секреты (`.env`) в бэкап не входят — храните их отдельно в secret manager. Без `SECRETS_ENCRYPTION_KEYS` сохранённые ключи провайдеров из бэкапа не расшифровать (это намеренно).

### Бэкап

Основной режим (с этапа 08A) — автоматический: шифрование `age`, выгрузка вне сервера, расписание, ротация — **§16**. Ручной запуск с теми же настройками: `deploy/backup.sh`. Простой незашифрованный бэкап в указанный каталог без выгрузки и ротации (прежний режим):

```bash
deploy/backup.sh ./backups        # → ./backups/<UTC-время>/{db.dump, storage.tar.gz, MANIFEST, SHA256SUMS}
```

- `pg_dump -Fc` выполняется внутри контейнера `db`, приложение можно не останавливать (снимок БД консистентен).
- Сначала дампится БД, затем архивируется storage: файлы, на которые нет ссылок, допустимы, обратное — нет.
- Файлы создаются с правами только для владельца (`umask 077`); при ошибке неполный каталог удаляется и скрипт завершается с ненулевым кодом. `MANIFEST` содержит время, имя проекта Compose и ревизию схемы (`alembic_revision`).
- Для другого проекта Compose (второй стенд) задайте `COMPOSE_PROJECT_NAME` (и при необходимости `COMPOSE_FILE`, `COMPOSE_ENV_FILES`) — так работает `e2e/run.sh`.
- Бэкап содержит конфиденциальные договоры: для хранения вне сервера используйте режим §16 (шифрование и выгрузка выполняются автоматически). Удалённые в приложении документы остаются в бэкапах до их ротации.

Эквивалент вручную:

```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > db.dump
docker run --rm -v contract-ai-analyzer_storage:/data:ro contract-ai-analyzer/backup-tools:local tar -C /data -czf - . > storage.tar.gz
```

### Восстановление

```bash
deploy/restore.sh ./backups/<UTC-время> --confirm                                       # незашифрованный каталог
deploy/restore.sh backups/restore/<UTC-время>.tar.age --identity <ключ age> --confirm     # зашифрованный (§16.6)
```

Скрипт: проверяет контрольные суммы → проверяет, что ревизия схемы из `MANIFEST` известна текущей версии приложения (иначе код 3: восстанавливайте той версией, которой создан бэкап, затем обновляйтесь) → останавливает `frontend`, `backend`, `worker` → пересоздаёт схему `public` и выполняет `pg_restore --single-transaction --exit-on-error` → заменяет содержимое volume `storage` → `migrate` (доводит схему до текущей версии) → запуск. **Операция заменяет текущие данные**, поэтому без `--confirm` скрипт не выполняется.

После восстановления проверьте `curl -s http://127.0.0.1:8080/api/health/ready`, вход и выборочно открытие документов и отчётов. Восстановление на пустом стенде проверяется автоматически: `e2e/run.sh backup-restore` (§15).

- Вход после восстановления — паролями, действовавшими на момент бэкапа; сессии, созданные позже, недействительны.
- Ключи провайдеров расшифруются, только если `SECRETS_ENCRYPTION_KEYS` содержит ключ, действовавший на момент бэкапа (храните старые ключи столько же, сколько бэкапы, или вводите ключи провайдеров заново).
- Документы, удалённые после бэкапа, восстановятся вместе с ним: после восстановления повторите удаления, сделанные после даты бэкапа (их ID — в журнале аудита, `document.deleted`).

## 6. Логи

- Все сервисы пишут JSON в stdout; docker хранит до 5×10 МБ на контейнер (`docker compose logs -f backend worker`).
- В логах нет паролей, токенов, ключей API, URL подключения к БД с паролем и текста договоров: значения редактируются (`app/logging.py`), access-лог пишет только метод, путь без query-строки, статус и длительность.
- Трассировки исключений (`unhandled_exception`, `job_crashed`) содержат типы исключений и кадры стека, но **не текст сообщений** (`[message hidden]`): сообщение могло содержать фрагмент ответа модели или значения полей. Для отладки воспроизведите ошибку локально.
- Срок хранения: Docker хранит последние 5 файлов по 10 МБ на контейнер; для долгого хранения настройте сбор (Loki, ELK, journald) на хосте — логи не содержат текста договоров, но содержат IP и ID пользователей.
- Быстрая проверка утечек: `docker compose logs --no-color | grep -F '<фраза из договора>'` должен ничего не находить (на e2e-стенде автоматически: `e2e/run.sh check-logs`).
- Каждому запросу присваивается `request_id` (заголовок `X-Request-ID`, поле в логах и в ответах с ошибкой).

## 7. Production

- Запускать только за внешним TLS reverse proxy (Caddy/Traefik/nginx), который проксирует на `frontend:8080`; `HTTP_BIND=127.0.0.1`.
- `.env`: `APP_ENV=production`, `COOKIE_SECURE=true`, `ALLOWED_ORIGINS=https://<домен>`. Backend откажется стартовать с небезопасной конфигурацией (http-origin, `COOKIE_SECURE=false`, пустые ключи, ключ-заглушка, `LOG_LEVEL=DEBUG`).
- При `ENV=production`: OpenAPI выключен, ответы содержат HSTS.
- Контейнеры `backend`, `worker`, `converter`, `frontend` работают без root, с read-only FS, `cap_drop: ALL`, `no-new-privileges`. PostgreSQL не публикуется наружу; сети `data` и `convert` без выхода в интернет.
- Образ `ollama/ollama:latest` в профиле `ollama` не зафиксирован по версии — для production укажите конкретный тег.
- Backend доверяет `X-Forwarded-For` только от nginx (`FRONTEND_EDGE_IP`, по умолчанию `172.30.10.10` в подсети `EDGE_SUBNET` `172.30.10.0/24`); меняйте их вместе, если подсеть пересекается с сетями хоста. Внешний reverse proxy должен передавать nginx реальный адрес клиента, иначе лимит входа по IP будет общим (на Docker Desktop все клиенты видны как шлюз `172.30.10.1`).
- Ограничения ресурсов: `worker` 3 ГБ / 256 процессов, `backend` 1 ГБ / 256, `converter` 1 ГБ / 1 CPU / 128, `frontend` 256 МБ / 64 (`compose.yaml`).
- Меры безопасности и остаточные риски — [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md); отдельная роль БД для приложения — §14.7.
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
- Неверный текущий пароль при смене пароля считается неудачной попыткой входа по этому e-mail (аудит `auth.password_change_failed`): после `LOGIN_MAX_FAILURES_PER_EMAIL` неудач и смена пароля, и вход временно отклоняются (429). Снять блокировку — «Снять блокировку входа» у пользователя или дождаться окна.

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


## 11. Модели и провайдеры (OpenRouter, ProxyAPI, Ollama)

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

### ProxyAPI

Российский агрегатор (proxyapi.ru): оплата в рублях, модели OpenAI, Anthropic, Google, DeepSeek и др. через один OpenAI-совместимый API. Облачный провайдер — подпадает под выключатель облака, предупреждение, маскировку и подтверждение передачи так же, как OpenRouter.

1. Ключ — в личном кабинете `console.proxyapi.ru`. Superadmin: «Добавить провайдера» → тип ProxyAPI, адрес по умолчанию `https://api.proxyapi.ru/v1`, API-ключ.
2. «Проверить соединение» — запрос `GET /models` без генерации. Баланс (`/proxyapi/balance`) приложение не запрашивает.
3. «Импорт моделей» — список из `GET /models` (идентификатор вида `openai/gpt-4o-mini`, контекст — если сообщён). **Цен в списке нет**: задайте цены в карточке модели вручную, валюта RUB, по каталогу на сайте ProxyAPI (за 1 млн токенов). Без цен оценка стоимости анализа не показывается. Контекстное окно ProxyAPI тоже не сообщает — при необходимости задайте его в карточке модели. «Проверить модель» запрашивает `GET /models/<идентификатор>`.
4. «Проверить с генерацией» (**тарифицируется**) — как у OpenRouter. Строгий режим JSON Schema есть не у всех моделей: при отказе выберите JSON-режим.
- Рассуждения «выключены» передаются как `reasoning_effort: "minimal"`; для моделей без рассуждений оставляйте «включён» (параметр не передаётся) — иначе ProxyAPI может вернуть 400.
- Ограничения ProxyAPI: 402 — недостаточно средств или исчерпан бюджет ключа (оценка баланса идёт по максимальной длине ответа — задавайте разумный «Максимум токенов ответа»); 20 ответов 4xx за минуту ставят ключ на паузу (429 от 1 минуты до 12 часов).
- Логирование запросов в кабинете ProxyAPI по умолчанию выключено; если его включить, ProxyAPI хранит полный текст запросов и ответов (фрагменты договоров) выбранный срок — для работы с договорами не включайте.
- Проверено только тестами с подменой сети (2026-10-29); live-вызовы ProxyAPI не выполнялись.

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

Анализ выполняет worker (задача `run_analysis`); API только проверяет запрос и ставит задачу. Запуск и отчёт — в UI (§13) или через API (ниже). Результат не заменяет консультацию юриста — это указано в каждой сводке.

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

## 13. Интерфейс анализа и экспорт отчётов

### Работа в UI
- **Запуск**: «Договоры» → карточка договора → «Запустить анализ» (или «Анализы» → «Новый анализ»). Выберите сторону, категории, глубину и модель; для облачной модели прочитайте предупреждение и подтвердите передачу. «Проверить модель и объём» показывает число частей, верхнюю оценку токенов, лимит и стоимость (только если цена модели известна). Повторное нажатие «Запустить» не создаёт второй анализ (один `Idempotency-Key` на форму).
- **Ход анализа** обновляется автоматически (каждые 2,5 с); отмену и повтор видят только пользователи с правом анализа документа. Viewer видит отчёт и может выгрузить его, но не запускает анализ.
- **Отчёт**: «Подтверждён» — цитата найдена и подтверждает риск по оценке верификатора; «Требует проверки» — смысловая связь, отсутствующее условие или противоречие нужно проверить; «Не подтверждён» — скрыт по умолчанию («Показывать отклонённые»). «Открыть в тексте» переходит к фрагменту договора.

### Экспорт
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `EXPORT_TTL_HOURS` | 168 | сколько часов хранится сформированный файл; затем worker удаляет файл (статус «срок хранения истёк») — сформируйте заново |
| `MAX_PENDING_EXPORTS_PER_USER` | 10 | экспортов одного пользователя в очереди (429 `too_many_pending_exports`) |

- Форматы: «Отчёт PDF», «Отчёт DOCX» (полный отчёт), «Риски CSV», «Риски XLSX» (список рисков со статусами). Файлы формирует worker (задача `build_export`, обычно 1–3 с) в `storage/exports/`.
- Файл экспорта содержит текст договора (цитаты) — обращайтесь с ним как с договором. Название договора в имени файла не используется (`contract-analysis-<id>.<ext>`).
- В CSV/XLSX значения, похожие на формулы (`=`, `+`, `-`, `@` в начале), записываются с префиксом `'` — это защита, а не ошибка данных.
- Диагностика: `export_not_ready` (409) — файл ещё формируется; `export_expired` (410) — срок хранения истёк; `analysis_not_finished` (409) — анализ не завершён; статус «ошибка формирования» — см. лог worker'а (`job_failed`, код). В логах — только ID экспорта, формат и размер; аудит — `export.created`, `export.downloaded`.

### Настройки
Раздел «Настройки» (Admin/Superadmin) показывает облачный выключатель и версию предупреждения (изменяются в «Модели») и действующие системные параметры из окружения — только для чтения; изменение — через `.env` и перезапуск backend/worker.

## 14. Обслуживание и аварийное восстановление

### 14.1 Проверки состояния

| Эндпоинт | Что проверяет | Использование |
|---|---|---|
| `GET /api/health/live` | процесс backend отвечает | healthcheck контейнера backend |
| `GET /api/health/ready` | БД, схема на `head`, есть активный Superadmin, хранилище доступно на запись (200/503); отдельно `checks.worker` — heartbeat worker'а не старше 120 с | мониторинг, страница «Состояние системы» |
| `python -m app.worker --healthcheck` | heartbeat-файл worker'а | healthcheck контейнера worker |
| `GET /nginx-health` | nginx | healthcheck контейнера frontend |

`checks.worker: unavailable` не делает API неготовым, но загрузки, анализы и экспорт не выполняются: `docker compose ps worker`, `docker compose logs worker`. Эндпоинты health не обращаются к LLM-провайдерам.

### 14.2 Лимиты и восстановление после сбоев

| Что ограничено | Параметр (по умолчанию) | При превышении |
|---|---|---|
| Размер файла | `MAX_UPLOAD_MB` (25) и `client_max_body_size` nginx (30m) | 413 |
| Страниц в документе | `MAX_PAGES` (500) | `too_many_pages` |
| Время и память разбора | `EXTRACTION_TIMEOUT_SECONDS` (180), `EXTRACTION_MEMORY_MB` (1024); LibreOffice — `LIBREOFFICE_TIMEOUT_SECONDS` (90) | процесс убивается, `extraction_timeout` / `resource_limit_exceeded` |
| Документов в очереди извлечения на пользователя | `MAX_PENDING_EXTRACTIONS_PER_USER` (10) | 429 |
| Токенов на анализ (бюджет) | `ANALYSIS_MAX_TOKENS` (1,5 млн) | 422 при запуске по оценке; во время работы — оставшиеся части пропускаются |
| Длина контекста | контекст модели из каталога − инструкции − лимит ответа | 422 `model_context_too_small`; текст делится на части по контексту |
| Активных анализов на пользователя | `ANALYSIS_MAX_ACTIVE_PER_USER` (3) | 429 |
| Параллельных задач worker'а | `WORKER_CONCURRENCY` (2); вызовов провайдера — «Параллельность» провайдера (в пределах процесса) | задачи ждут в очереди |
| Время вызова модели | «Таймаут» провайдера (5–900 с), до 3 повторов внутри вызова, до 4 попыток задачи | `failed` с кодом |
| Экспортов в очереди | `MAX_PENDING_EXPORTS_PER_USER` (10) | 429 |
| Ресурсы контейнеров | `mem_limit`, `pids_limit`, `cpus` в `compose.yaml` | OOM-kill контейнера, перезапуск (`restart: unless-stopped`) |

Восстановление: задачи хранятся в PostgreSQL с lease (`JOB_LEASE_SECONDS`, 120 с). Если worker остановлен аварийно (OOM, `kill -9`, перезагрузка хоста), задача возвращается в очередь после истечения lease (reaper — раз в минуту), и анализ продолжается с незавершённой части (проверено e2e: `SIGKILL` worker'а посреди анализа → анализ завершён через ≈ 2,6 мин). При штатной остановке (`docker compose stop`) worker дожидается текущих задач до `stop_grace_period` (60 с). Отмена (`POST /api/analyses/{id}/cancel`) прерывает текущий вызов модели; «Повторить» продолжает без пересчёта готовых частей.

### 14.3 Ротация секретов

| Секрет | Как сменить | Последствия |
|---|---|---|
| `SECRET_KEY` | новое значение (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) в `.env` → `docker compose up -d backend worker` | все сессии завершаются, счётчики неудачных входов сбрасываются; пользователи входят заново |
| `SECRETS_ENCRYPTION_KEYS` | §11 «Ротация ключа шифрования секретов» (новый ключ первым → `python -m app.cli rotate-secrets` → удалить старый). Команда перешифровывает и данные маскировки (§17): найденные данные предпросмотров и таблицы соответствий анализов | без перерыва; старый ключ храните, пока живы бэкапы, сделанные при нём |
| `POSTGRES_PASSWORD` | `docker compose exec db psql -U cai -d cai -c "ALTER USER cai PASSWORD '<новый hex-пароль>'"` → новое значение в `.env` → `docker compose up -d backend worker` | кратковременные ошибки подключения до перезапуска. `POSTGRES_PASSWORD` образа применяется только при создании пустой БД — менять его в `.env` без `ALTER USER` нельзя |
| Ключ OpenRouter / ProxyAPI | UI «Модели» → провайдер → новый ключ (только Superadmin); старый отозвать в кабинете провайдера | сразу для новых вызовов |
| `BOOTSTRAP_ADMIN_PASSWORD` | после первого входа удалить из `.env`, `BOOTSTRAP_ENABLED=false` (§9) | — |
| Пароль пользователя | сам пользователь — «Мой профиль»; администратор — «Сбросить пароль» (временный, обязательная смена) | сессии пользователя завершаются |

Права на `.env` — только владелец (`chmod 600 .env`). Секреты храните в secret manager отдельно от бэкапов данных.

### 14.4 Обновления и миграции

1. Прочитайте изменения (миграции — `backend/app/db/migrations/versions/`; разрушающие операции помечены `# destructive-approved:`).
2. Бэкап: `deploy/backup.sh ./backups` (и копия вне сервера).
3. Новая версия исходников → `docker compose build`.
4. `docker compose run --rm migrate` — миграция в одной транзакции под advisory lock; при ошибке схема не меняется.
5. `docker compose up -d` → `curl -s http://127.0.0.1:8080/api/health/ready` (все проверки `ok`) → вход и открытие отчёта.
6. Откат: верните прежние исходники и образы; если новая миграция уже применена — восстановите бэкап из п. 2 прежней версией (`deploy/restore.sh`). `alembic downgrade` в production запрещён без `ALLOW_DOWNGRADE=1` и бэкапа.

Перед обновлением production прогоните новую версию на стенде: `e2e/run.sh all` (§15).

### 14.5 Очистка документов и данных

- Документ удаляет владелец или Superadmin: карточка договора → «Удалить документ» (или `DELETE /api/documents/{id}`). Удаляются оригинал, DOCX после конвертации, извлечённый текст, фрагменты, анализы, риски и экспорты; в аудите остаются ID и SHA-256.
- Экспорты удаляются автоматически через `EXPORT_TTL_HOURS`.
- **Автоматическое удаление по сроку (политика хранения)** — по умолчанию выключено. Superadmin: «Настройки» → «Хранение документов» → «Удалять документы автоматически» → срок в днях (1–3650) → предупреждение с числом документов, которые будут удалены сразу → подтверждение → «Сохранить». API: `GET /api/admin/retention?preview_days=N` (Admin+, состояние и предпросмотр), `PUT /api/admin/retention` `{"retention_days": N, "confirm": true}`; выключение — `{"retention_days": null}` (подтверждение не нужно, как и при увеличении срока).
  - Удаляются документы **без активности** дольше срока: загружены раньше срока, нет новых версий и анализов новее срока. Документ с анализом в очереди или в работе не удаляется до его завершения.
  - Worker проверяет политику раз в минуту и удаляет до 100 документов за проход тем же путём, что и ручное удаление (файлы, текст, анализы, экспорты). В аудите — `document.deleted` без автора с `reason: retention` и сроком; изменение настройки — `settings.updated` и история конфигурации. В логе worker'а — `retention_documents_deleted` (число).
  - Выключение действует сразу: документы, уже переведённые в удаление, удаляются, новые — нет.
  - Срок хранения бэкапов должен соответствовать политике: удалённые документы остаются в бэкапах до их ротации.
- Ручная очистка (при выключенной политике или выборочно). Найти давние документы:

  ```bash
  docker compose exec db psql -U cai -d cai -c "SELECT d.id, u.email AS owner, d.created_at FROM documents d JOIN users u ON u.id = d.owner_id WHERE d.status = 'active' AND d.created_at < now() - interval '365 days' ORDER BY d.created_at"
  ```

  и удалите их через UI/API от имени владельца или Superadmin. Удаление строк через SQL не удаляет файлы — не используйте его.
- Файлы в хранилище без записей в БД (например, после сбоя во время загрузки) не опасны, но занимают место; их можно найти сравнением `storage/originals/` с `document_versions.storage_key` — удаляйте только после бэкапа.
- **Данные удалённых документов остаются в бэкапах** до ротации бэкапов: срок хранения бэкапов — это срок, в течение которого удалённый договор ещё можно восстановить.
- Журнал аудита не очищается (append-only, триггер БД).

### 14.6 Отключение облачных моделей

- Штатно и аварийно: Superadmin → «Модели» → «Отключить облачные модели» (или `PUT /api/admin/llm-settings` с `{"cloud_models_enabled": false}`). Новые облачные анализы отклоняются, идущие останавливаются перед ближайшим вызовом модели (`failed`, `cloud_models_disabled`), полученные результаты сохраняются.
- Дополнительно можно выключить провайдера OpenRouter или удалить его ключ; при компрометации ключа — сразу отозвать его в кабинете OpenRouter.
- Проверка: `GET /api/models/available` у обычного пользователя не содержит облачных моделей; в аудите — `settings.updated`.
- После повторного включения пользователи подтверждают передачу заново при каждом облачном анализе; предупреждение нужно принять заново, только если изменилась его версия.

### 14.7 Роль БД для production (рекомендация)

По умолчанию backend подключается пользователем `POSTGRES_USER`, который в образе `postgres` является суперпользователем и может отключить триггер, защищающий журнал аудита. Для повышенных требований создайте отдельную роль без прав суперпользователя: владелец схемы (для `migrate`) — `cai`; приложению (`DATABASE_URL` у backend и worker) — роль с `SELECT, INSERT, UPDATE, DELETE` на таблицы (без `UPDATE, DELETE` на `audit_log`) и `USAGE` на последовательности. Это изменение развёртывания — проверьте его на стенде (`e2e/run.sh`).

### 14.8 Смена URL Ollama

1. Проверьте, что новый адрес доступен из контейнеров: `docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://<адрес>:11434/api/version', timeout=5).read())"`.
2. Superadmin → «Модели» → провайдер Ollama → изменить адрес; для адреса в частной сети — «Разрешить частную сеть» (таблица §11). Сохранённый API-ключ при смене адреса сбрасывается (введите заново, если Ollama за прокси с авторизацией).
3. «Проверить соединение» → для каждой модели «Проверить с генерацией». Модели должны быть скачаны на новом сервере (`ollama pull <модель>`); приложение их не скачивает.
4. Изменение применяется сразу, без перезапуска; идущие анализы используют новый адрес со следующего вызова модели. Смена записывается в историю конфигурации и аудит.
5. Публичный адрес (интернет, только `https`) делает провайдера облачным: на него распространяются выключатель облака и обязательное согласие пользователей.

### 14.9 Аварийное восстановление

| Ситуация | Действия |
|---|---|
| Потерян хост | Новый хост с Docker → исходники той же версии → `.env` из secret manager (те же `SECRET_KEY`, `SECRETS_ENCRYPTION_KEYS`, `POSTGRES_*`) → `docker compose up -d --build` → `deploy/restore.sh <последний бэкап> --confirm` → проверки §14.4 п. 5. Данные после последнего бэкапа потеряны — сообщите пользователям, что загрузить заново |
| Повреждена БД или ошибочно изменены данные | `docker compose stop backend worker` → бэкап текущего состояния для разбора → восстановление последнего корректного бэкапа |
| Потерян `SECRETS_ENCRYPTION_KEYS` | Остальные данные не затронуты: новый ключ → перезапуск backend/worker → ввести ключи провайдеров заново (до этого `secret_undecryptable`) |
| Потерян или скомпрометирован `SECRET_KEY` | Новый ключ (§14.3): все сессии завершатся |
| Скомпрометирован ключ OpenRouter | Отозвать ключ у провайдера → «Отключить облачные модели» → новый ключ → проверить расход в кабинете провайдера |
| Скомпрометирована учётная запись | «Заблокировать» (завершает сессии) → «Сбросить пароль» → проверить журнал аудита по пользователю (входы, скачивания, экспорты) |
| Утерян пароль единственного Superadmin | На сервере: `docker compose exec backend python -m app.cli reset-superadmin-password --email <e-mail>` — выводит временный пароль один раз, завершает сессии, снимает блокировку входа, пишет `user.password_reset` (`via: cli`) в аудит; при входе пароль нужно сменить |
| Нет ни одного активного Superadmin | Readiness `accounts: bootstrap_required` — задайте `BOOTSTRAP_ADMIN_*` и перезапустите backend (§9) |
| Worker не обрабатывает задачи | `docker compose ps worker`, `docker compose logs worker`; после перезапуска задачи подхватываются автоматически (§14.2) |
| Converter завис | `docker compose restart converter`; извлечение повторится (`converter_unavailable` — временная ошибка) |

Регулярно (например, раз в квартал) проверяйте восстановление бэкапа на отдельном стенде — так, как это делает `e2e/run.sh backup-restore`.

## 15. End-to-end проверка на отдельном стенде

`e2e/run.sh` поднимает **отдельный** проект Compose `cai-e2e` (свои volumes, подсеть `172.30.91.0/24`, порт `127.0.0.1:8091`, случайные секреты в `e2e/.env.e2e`) и mock-сервер с API Ollama (`e2e/mock-llm/server.py`: детерминированные ответы, тексты запросов не логирует). Основной стенд и его данные не затрагиваются. В приложении нет «тестовых» провайдеров: mock подключается как обычный провайдер Ollama в частной сети.

```bash
(cd frontend && npm ci && npx playwright install chromium)   # один раз
e2e/run.sh all       # up → flow → recovery → check-logs → backup-restore → down
```

| Шаг | Что проверяется |
|---|---|
| `up` | сборка образов, миграции на пустой БД, bootstrap Superadmin из окружения, readiness |
| `flow` | заголовки безопасности и CSP, закрытый API без сессии, отказ входа с чужим Origin; UI: вход → обязательная смена пароля → провайдер и модель (проверка соединения через SSRF-guard, тестовая генерация подтверждает JSON Schema) → загрузка PDF → извлечение (2 стр., SHA-256) → новый анализ с оценкой объёма → отчёт: два «Подтверждён» (дословные цитаты, стр. 1, п. 2.2), «Требует проверки» для отсутствующего условия, выдуманная цитата — «Не подтверждён» и скрыта по умолчанию, предупреждение о prompt injection → API: цитаты — подстроки сохранённого текста фрагментов → экспорт PDF и XLSX через UI → аудит без текста договора; axe-core (WCAG 2.1 A/AA) на 6 страницах |
| `recovery` | отмена во время вызова модели → `cancelled` → повтор → `completed`; `SIGKILL` worker'а посреди анализа → после истечения lease анализ завершается с подтверждёнными рисками |
| `check-logs` | в логах всех контейнеров нет фраз договора, паролей, `SECRET_KEY`, пароля БД, ключа шифрования |
| `backup-restore` | `deploy/backup.sh` → стенд пересоздаётся с пустыми volumes → `deploy/restore.sh` → вход со сменённым паролем, SHA-256 оригинала, анализ и подтверждённые риски на месте |
| `down` | удаление проекта `cai-e2e` вместе с его volumes |

Отдельные шаги: `e2e/run.sh up|flow|recovery|check-logs|backup-restore|down`. При падении стенд остаётся для разбора (`docker compose -p cai-e2e ps`, отчёт Playwright — `frontend/playwright-report/`), удаление — `e2e/run.sh down`. Фикстуры — синтетические договоры в `e2e/fixtures/` (генерация: `e2e/make_fixtures.py`).

## 16. Автоматическое резервное копирование (шифрование, выгрузка вне сервера, расписание)

### 16.1 Как это устроено

| Кто | Что делает |
|---|---|
| Планировщик на хосте (systemd timer или cron, `deploy/backup-schedule.sh`) | раз в 5 минут запускает `deploy/backup.sh --scheduled` |
| `deploy/backup.sh` на хосте | спрашивает у приложения, пора ли (`python -m app.cli backup-plan`); делает `pg_dump` и архив storage, шифрует (`age`), выгружает (`rclone` для S3 или копия в каталог), проверяет контрольную сумму на стороне хранилища, удаляет старые копии, сообщает результат приложению (`app.cli backup-report`) и при ошибке — на webhook |
| Приложение | хранит несекретные настройки, которые Superadmin меняет в UI; считает расписание; показывает состояние и журнал запусков («Настройки» → «Резервное копирование») |
| Образ `contract-ai-analyzer/backup-tools:local` (`deploy/backup-tools`, Alpine 3.22: age 1.2, rclone 1.69, curl) | инструменты шифрования и выгрузки; на хост ничего не устанавливается, образ собирается автоматически при первом запуске |

Разделение доверия: пути, адрес и ключи хранилища, публичный ключ шифрования и webhook задаются **только в `.env` (или `deploy/backup.env`) на сервере**. Через веб-интерфейс их изменить нельзя — иначе взлом учётной записи Superadmin позволил бы перенаправить бэкапы или ключи хранилища. Ответ приложения скрипт читает как данные (белый список ключей, проверка каждого значения), не исполняет; каждый параметр повторно проверяется на хосте. Docker socket контейнерам не выдаётся: команды `docker` выполняет хост. Ключи S3 и URL webhook в контейнеры приложения не передаются и не попадают в аргументы команд (только в окружение контейнера инструментов).

### 16.2 Параметры

| Параметр | Значения | По умолчанию | Где можно изменить |
|---|---|---|---|
| `BACKUP_TARGET` | `none` — только копии на сервере; `s3` — S3-совместимое хранилище; `local` — каталог (NAS, второй диск) | `none` | `.env`, UI (только из настроенных на сервере) |
| `BACKUP_SCHEDULE` | cron, 5 полей (`0 3 * * *`, `0 */6 * * *`, `@daily`); `off` — только ручной запуск | `0 3 * * *` | `.env`, UI |
| `BACKUP_TIMEZONE` | часовой пояс расписания (`Europe/Moscow`) | `UTC` | `.env`, UI |
| `BACKUP_KEEP_LOCAL` | число копий на сервере (≥ 1) | `7` | `.env`, UI |
| `BACKUP_KEEP_REMOTE_DAILY` / `_WEEKLY` / `_MONTHLY` | схема хранения во внешнем хранилище: последний бэкап каждого из N дней / недель (ISO) / месяцев | `7` / `4` / `6` | `.env`, UI |
| `BACKUP_REMOTE_PRUNE` | `script` — старые внешние копии удаляет скрипт по схеме; `lifecycle` — удаление правилами хранилища, скрипт ничего не удаляет | `script` для `local`, `lifecycle` для `s3` | `.env`, UI |
| `BACKUP_DIR` | каталог локальных копий | `./backups` | только `.env` |
| `BACKUP_ENCRYPTION_RECIPIENT` | публичный ключ `age` (`age1…`); обязателен для `s3`/`local`. Если задан, шифруются и локальные копии | — | только `.env` |
| `BACKUP_LOCAL_TARGET_DIR` | для `local`: абсолютный путь, отличный от `BACKUP_DIR` | — | только `.env` |
| `BACKUP_S3_ENDPOINT`, `BACKUP_S3_BUCKET`, `BACKUP_S3_PREFIX`, `BACKUP_S3_REGION` | хранилище для `s3` | — | только `.env` |
| `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` | ключи доступа (секрет; не логируются, в контейнеры приложения не передаются) | — | только `.env` |
| `BACKUP_NOTIFY_WEBHOOK` | URL для POST JSON `{"event":"backup_failed","project","error_code","time"}` при ошибке; пусто — только журнал и код выхода | пусто | только `.env` |
| `BACKUP_MIN_INTERVAL_MINUTES` | не чаще одного бэкапа по расписанию или запросу из UI за этот интервал (защита от слишком частых запусков); на ручной запуск не действует | `60` | только `.env` |
| `BACKUP_DOCKER_NETWORK` | необязательно: сеть Docker для контейнеров выгрузки и уведомлений (если хранилище — контейнер в этой сети) | пусто | только `.env` |

Значение из UI имеет приоритет над `.env`; «вернуть значение из .env» снимает переопределение. Некорректное значение (в `.env` или в UI) останавливает бэкап с понятной ошибкой: в UI — список проблем, в журнале — сообщение, по webhook — `config_invalid` (не чаще раза в сутки). UI не сохраняет изменение, которое делает рабочие настройки некорректными (например, цель s3, не настроенную на сервере). Изменения сроков хранения действуют со следующего запуска.

### 16.3 Первоначальная настройка

1. Ключ шифрования — **на компьютере администратора, не на сервере**: `docker run --rm contract-ai-analyzer/backup-tools:local age-keygen` (или `age-keygen -o backup-key.txt`). Строку `# public key: age1…` запишите в `BACKUP_ENCRYPTION_RECIPIENT`; файл с `AGE-SECRET-KEY-…` храните в менеджере паролей/сейфе (лучше две копии у разных людей). Без него бэкапы не восстановить.
2. Хранилище:
   - **S3**: создайте bucket и ключ с правами только на этот bucket (желательно без права удаления — см. 16.5); задайте `BACKUP_TARGET=s3`, `BACKUP_S3_*`. Проверено с S3-совместимым сервером `rclone serve s3`; с AWS S3, Yandex Object Storage, MinIO и др. — по протоколу S3 (`provider=Other`, path-style), без живой проверки.
   - **Каталог**: смонтируйте NAS/второй диск, задайте `BACKUP_TARGET=local`, `BACKUP_LOCAL_TARGET_DIR=/mnt/backup/cai`.
3. Проверка: `deploy/backup.sh --check` → `deploy/backup.sh` (первый бэкап сейчас) → `deploy/backup.sh --list`.
4. Расписание (Linux-хост, пользователь в группе `docker` или root): `sudo deploy/backup-schedule.sh install` (systemd timer; без systemd — строка crontab: `deploy/backup-schedule.sh install --cron`). `status` — состояние, `uninstall` — удаление. Переустанавливать после изменения расписания не нужно: таймер лишь опрашивает приложение.
5. В UI «Настройки» → «Резервное копирование»: «Планировщик на сервере опрашивал настройки» — время последнего опроса (предупреждение, если больше 15 минут не было), следующий запуск, последний успешный бэкап, журнал запусков.

### 16.4 Что делает один запуск

1. Блокировка (`flock` или каталог с PID): второй ручной запуск завершается с кодом 3, опрос планировщика — тихо пропускается.
2. Настройки и признак «пора» от приложения; при `--scheduled` — только если наступило время по расписанию (пропущенное время догоняется один раз) или Superadmin нажал «Запустить сейчас», и прошло не меньше `BACKUP_MIN_INTERVAL_MINUTES`. Если приложение недоступно, ручной запуск использует значения из `.env`, а опрос планировщика завершается ошибкой `app_unavailable`.
3. `pg_dump -Fc`, архив volume storage, `MANIFEST` (ревизия схемы), `SHA256SUMS` → один tar → `age -r <ключ>` → `BACKUP_DIR/<UTC-время>.tar.age` и `.sha256` (права только владельца). Без ключа — незашифрованный каталог `BACKUP_DIR/<UTC-время>/`.
4. Выгрузка: `local` — копирование и сверка SHA-256 копии; `s3` — `rclone copyto` и сверка MD5 объекта в хранилище (если хранилище MD5 не сообщает — сверка скачанной копии).
5. Только после успешной выгрузки: удаление локальных копий сверх `BACKUP_KEEP_LOCAL` и (при `script`) внешних копий вне схемы daily/weekly/monthly. **Самый новый бэкап не удаляется никогда; при ошибке выгрузки локальные копии не удаляются.**
6. Отчёт приложению (журнал в UI, аудит `backup.completed` / `backup.failed`), журнал `BACKUP_DIR/backup.log`; при ошибке — webhook с кодом этапа: `dump_failed`, `encrypt_failed`, `upload_failed`, `verify_failed`, `prune_failed`, `config_invalid`, `app_unavailable`. Запуск, не приславший результат за 6 часов, отмечается как «прерван».

### 16.5 Ротация во внешнем хранилище и защита от взлома сервера

- `lifecycle` (по умолчанию для S3) — скрипт ничего не удаляет; настройте правила хранилища: удаление объектов с префиксом `BACKUP_S3_PREFIX/` старше N дней (например, 190 дней ≈ 6 месяцев), versioning и, если поддерживается, object lock (WORM). Выдайте ключу права только `PutObject`, `GetObject`, `ListBucket` — тогда взломщик сервера не сможет удалить или перезаписать старые копии.
- `script` — удобно для каталога на NAS; на S3 требует права на удаление, т.е. компрометация сервера позволяет стереть копии.
- Срок хранения бэкапов = срок, в течение которого удалённый в приложении договор ещё можно восстановить (§14.5).

### 16.6 Восстановление из зашифрованной копии

```bash
deploy/backup.sh --list                                  # копии на сервере и во внешнем хранилище
deploy/backup.sh --fetch 20261017T030000Z.tar.age        # скачать из хранилища в BACKUP_DIR/restore/ (с проверкой SHA-256)
# временно скопируйте приватный ключ на сервер (права 600), затем:
deploy/restore.sh backups/restore/20261017T030000Z.tar.age --identity /root/backup-key.txt --confirm
shred -u /root/backup-key.txt                            # удалите ключ с сервера
```

`restore.sh` проверяет `.sha256`, расшифровывает в контейнере инструментов, проверяет `SHA256SUMS` и ревизию схемы и выполняет обычное восстановление (§5). Незашифрованные каталоги восстанавливаются как раньше. На новом сервере: исходники той же версии, `.env` из secret manager, `docker compose up -d --build`, затем шаги выше (§14.9). Проверяйте восстановление регулярно: `e2e/run.sh backup-offsite` делает полный цикл на отдельном стенде.

### 16.7 Ротация ключа шифрования

1. Новая пара ключей у администратора; новый публичный ключ — в `BACKUP_ENCRYPTION_RECIPIENT` (перезапуск backend, чтобы UI показал новый ключ: `docker compose up -d backend`).
2. Новые бэкапы шифруются новым ключом; **старый приватный ключ храните, пока во внешнем хранилище есть копии, зашифрованные им** (по схеме хранения — до 6 месяцев).
3. При компрометации приватного ключа: новая пара, внеочередной бэкап («Запустить сейчас»), удаление старых копий из хранилища (они читаемы с утёкшим ключом).

### 16.8 Диагностика

| Признак | Причина и действие |
|---|---|
| В UI «Планировщик … не опрашивал настройки» | таймер не установлен или не работает: `deploy/backup-schedule.sh status`, `journalctl -u contract-ai-analyzer-backup.service` |
| Код выхода 2, `config_invalid` | некорректные параметры — сообщения в журнале и в UI; `deploy/backup.sh --check` |
| `upload_failed` | хранилище недоступно, неверные ключи, нет прав; локальная копия сохранена — повторите после исправления |
| `verify_failed` | контрольная сумма копии в хранилище не совпала — проверьте хранилище, повторите бэкап |
| `app_unavailable` | `backup-plan` не выполнился: стек остановлен или БД недоступна (`docker compose ps`) |
| Бэкап «просрочен» в UI | последний запуск по расписанию (с запасом 2 ч) не завершился успешно — журнал запусков и `BACKUP_DIR/backup.log` |

## 17. Маскировка данных перед облачным анализом (этап 10)

Архитектура — ARCHITECTURE §22, план и решения — [REDACTION_PLAN.md](REDACTION_PLAN.md).

### 17.1 Как это работает для пользователя

- При выборе **облачной** модели кнопка запуска заменяется на «Проверить маскировку». Worker ищет данные (задача `redaction_scan`), открывается экран предпросмотра: текст с подсветкой по категориям, список найденных значений (снять пометку, сменить категорию, отдельные вхождения), блок «Проверьте» (низкая уверенность), ручная пометка выделением текста («пометить все вхождения»), переключатель «Как увидит модель». Запуск — после отметки «Подсвеченные данные проверены».
- Для **локальных** моделей (Ollama в частной сети) маскировки и предпросмотра нет.
- Кнопка «Отправить без маскировки» видна, только если Superadmin включил это в настройках; отправка — через отдельное окно с предупреждением и обязательной отметкой.
- В отчёте и экспорте — строка «При анализе скрыто значений: N (категории…)» или «Отправлено без маскировки».

### 17.2 Настройки («Настройки» → «Маскировка данных», `GET/PUT /api/admin/redaction`)

- Читают Admin и Superadmin, меняет только Superadmin. Каждое изменение — ревизия (`config_revisions`, сущность `setting/redaction`, без значений словаря и шаблонов) и аудит `redaction.settings_updated`. Изменение настроек делает неподтверждённые и неиспользованные предпросмотры устаревшими (пользователь проверяет заново).
- По умолчанию включены персональные данные и реквизиты, выключены коммерческие (суммы, ставки, даты, номера, кадастровые номера, объекты) и технические (URL, IP); учётные данные (логины/пароли/токены) — включены.
- `allow_unmasked` (по умолчанию выключено). Включение — только через окно предупреждения с подтверждением (`confirm_allow_unmasked: true`, иначе 409 `redaction_unmasked_confirmation_required`); аудит `redaction.unmasked_allowed`/`redaction.unmasked_disallowed`.
- Словарь (до 500 фраз, поиск с учётом падежей), шаблоны (до 30, RE2: без обратных ссылок и lookaround; шаблон, совпадающий с пустой строкой, отклоняется — 422 `redaction_invalid_pattern`), исключения (до 500), срок предпросмотра (1–168 ч, по умолчанию 24).

### 17.3 Параметры окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `REDACTION_SCAN_TIMEOUT_SECONDS` | 300 | таймаут дочернего процесса поиска данных |
| `REDACTION_SCAN_MEMORY_MB` | 2048 | `RLIMIT_AS` дочернего процесса (NER + numpy; в Linux) |

`mem_limit` worker'а в `compose.yaml` (3 ГБ) рассчитан на `WORKER_CONCURRENCY=2`: при одновременном поиске в двух предпросмотрах и разборе документа увеличьте его.

### 17.4 Хранение и обслуживание

- Найденные значения (`redaction_previews.entities_encrypted`) и таблицы соответствий (`analysis_redactions.mapping_encrypted`) шифруются ключом `SECRETS_ENCRYPTION_KEYS`; открыто хранятся только счётчики по категориям. При запуске анализа найденные данные предпросмотра удаляются, остаётся таблица соответствий анализа.
- Housekeeping worker'а (раз в минуту) удаляет просроченные предпросмотры, не связанные с анализом. Удаление документа удаляет предпросмотры и таблицы каскадом.
- Ротация ключа шифрования: §14.3 (`python -m app.cli rotate-secrets` выводит отдельную строку «Данные маскировки: перешифровано N»).
- Модели NER (slovnet/navec, ≈ 29 МБ) и словари pymorphy3 входят в образ backend; загрузки из сети при работе нет. Размер образа backend: см. IMPLEMENTATION_STATUS, этап 10.

### 17.5 Диагностика

| Симптом | Причина и действие |
|---|---|
| Предпросмотр «Не удалось выполнить поиск данных» (`redaction_scan_timeout` / `resource_limit_exceeded`) | очень длинный документ: увеличьте `REDACTION_SCAN_TIMEOUT_SECONDS` / `REDACTION_SCAN_MEMORY_MB` (и `mem_limit` worker'а) |
| В предпросмотре предупреждение «Распознавание имён и названий недоступно» | в образе нет файлов моделей пакета `natasha` — пересоберите образ по `requirements.lock` |
| `503 secrets_encryption_not_configured` при создании предпросмотра | не задан `SECRETS_ENCRYPTION_KEYS` |
| Запуск облачного анализа — 409 `redaction_settings_changed` / `redaction_preview_expired` | пользователь проверяет маскировку заново (кнопка «Проверить заново») |
| Повтор облачного анализа — 409 `redaction_required_for_retry` | анализ запущен до этапа 10 без маскировки: запустите новый анализ |

## 18. Правила рисков организации (этап 11)

Архитектура — ARCHITECTURE §23, план и решения — [RISK_RULES_PLAN.md](RISK_RULES_PLAN.md).

### 18.1 Управление («Правила», `/admin/risk-rules`; API `/api/admin/risk-rules`)

- Создают, изменяют, включают и архивируют правила **Admin и Superadmin**; Analyst видит включённые правила только в форме анализа, Viewer — не видит.
- Поля: название (≤ 200), категория (одна из категорий анализа; без подходящей — «Другие риски»), где искать (риск в условии / отсутствующее условие / оба), что считать риском (≤ 1000), признаки и рекомендация (необязательно, ≤ 1000), рекомендуемый уровень (необязательно), «Включено», «Отмечено по умолчанию». Код `RR-NNN` выдаётся автоматически и не меняется.
- Не указывайте в правилах персональные и конфиденциальные данные: при облачном анализе текст правил передаётся провайдеру **без маскировки**.
- Правила не удаляются: «В архив» убирает правило из формы анализа (выполненные анализы хранят его текст в snapshot), «Восстановить» возвращает. История изменений — у каждого правила, аудит — группа «Правила рисков».
- Лимиты: до 100 неархивных правил в каталоге (409 `risk_rules_catalog_full`); одновременное изменение — 409 `version_conflict` (обновите страницу).

### 18.2 Применение в анализе

- В форме «Новый анализ» правила показаны под своей категорией; снятие категории снимает её правила. «Выбрать все»/«Снять все» действуют и на правила.
- На один анализ: до 20 правил, блок правил в промпте — до 8000 символов. Ошибки запуска (422): `risk_rule_unavailable` (правило выключено или в архиве — обновите форму), `risk_rule_category_not_selected`, `risk_rules_limit_exceeded`, `risk_rules_too_long`.
- Правила увеличивают объём каждого запроса анализа частей текста; правила типа «отсутствующее условие» запускают межраздельный проход, даже если категории «Противоречия» и «Отсутствующие условия» не выбраны. Доля правил видна в оценке объёма.
- В отчёте: бейдж «Правило RR-NNN · название» у риска, фильтр «Правило организации» (в т.ч. «Без правила»), в «Итогах» — «Применены правила организации: N». В CSV/XLSX — колонка «Правило», в PDF/DOCX — строка у риска и список правил в параметрах анализа.
- Отметка правила — вывод модели; к рискам по правилам применяются те же проверки цитат и статусы. Если модель указала код, которого нет в анализе, отметка снимается (у риска — пояснение).

