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

Журнал (UI «Журнал аудита», API `/api/admin/audit`, Admin и Superadmin) — append-only таблица `audit_log`: входы и неудачные входы (с IP и причиной, без введённых данных), выход и завершение сессий, смена/сброс пароля, создание пользователей, изменение ролей, блокировка, доступ к моделям, обращение Superadmin к чужим документам. Действия с документами, анализами, экспортом и моделями добавляются на этапах 04–07. Пароли, токены, ключи, тексты и названия договоров в журнал не пишутся.
