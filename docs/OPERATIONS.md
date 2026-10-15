# Эксплуатация

Документ дополняется на каждом этапе. Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Локальный запуск через Docker Compose

Требования: Docker Engine/Desktop с Compose v2, ~4 ГБ свободной памяти (образ converter с LibreOffice ~600 МБ).

```bash
cp .env.example .env
# Заполните POSTGRES_PASSWORD, SECRET_KEY, SECRETS_ENCRYPTION_KEYS (команды генерации — в .env.example)
docker compose up -d --build
docker compose ps                      # все сервисы healthy, migrate — exited (0)
curl -s http://127.0.0.1:8080/api/health/ready
```

Приложение: http://localhost:8080. При `APP_ENV=development` OpenAPI доступен на `/api/docs`.

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
