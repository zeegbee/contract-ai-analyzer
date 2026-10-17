#!/usr/bin/env bash
# Восстановление из каталога, созданного backup.sh. РАЗРУШИТЕЛЬНО: текущие данные БД и storage заменяются.
# Использование: deploy/restore.sh <каталог_бэкапа> --confirm   (запускать из корня репозитория)
# Восстанавливайте бэкап той же или более ранней версией приложения, чем текущая: после восстановления
# migrate доводит схему до текущей; бэкап из более новой версии (неизвестная ревизия) не восстанавливается.
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-contract-ai-analyzer}"
SRC="${1:-}"
if [[ -z "${SRC}" || "${2:-}" != "--confirm" ]]; then
  echo "Использование: $0 <каталог_бэкапа> --confirm" >&2
  echo "Восстановление заменит текущие данные. Сначала сделайте свежий бэкап." >&2
  exit 2
fi
SRC_ABS="$(cd "${SRC}" && pwd)"

echo "[restore] проверка контрольных сумм"
( cd "${SRC}" && sha256sum -c SHA256SUMS )

REVISION=""
if [[ -f "${SRC}/MANIFEST" ]]; then
  REVISION="$(grep -E '^alembic_revision=' "${SRC}/MANIFEST" | cut -d= -f2-)"
fi
if [[ -n "${REVISION}" ]]; then
  echo "[restore] ревизия схемы в бэкапе: ${REVISION}"
  # Ревизия должна быть известна текущей версии приложения (иначе migrate не сможет продолжить).
  if ! docker compose run --rm -T --no-deps migrate alembic history 2>/dev/null | grep -q "${REVISION}"; then
    echo "[restore] ревизия ${REVISION} неизвестна текущей версии приложения — восстановите бэкап" \
         "той версией, которой он создан" >&2
    exit 3
  fi
else
  echo "[restore] в бэкапе нет MANIFEST (создан до этапа 08) — проверка ревизии пропущена"
fi

echo "[restore] остановка приложения (БД остаётся запущенной)"
docker compose stop frontend backend worker
docker compose up -d db
docker compose exec -T db sh -c 'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do sleep 1; done'

echo "[restore] PostgreSQL"
# Схема пересоздаётся целиком: таблицы из более новой версии, которых нет в бэкапе, не должны остаться.
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SET client_min_messages = warning; DROP SCHEMA public CASCADE; CREATE SCHEMA public;"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --single-transaction --exit-on-error' < "${SRC}/db.dump"

echo "[restore] storage"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${PROJECT}_storage:/data" \
  -v "${SRC_ABS}:/backup:ro" \
  alpine:3 sh -c 'find /data -mindepth 1 -delete && tar -C /data -xzf /backup/storage.tar.gz'

echo "[restore] миграции и запуск"
docker compose run --rm migrate
docker compose up -d
echo "[restore] готово. Проверьте: curl -s http://127.0.0.1:\${HTTP_PORT:-8080}/api/health/ready"
