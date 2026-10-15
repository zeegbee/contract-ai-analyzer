#!/usr/bin/env bash
# Восстановление из каталога, созданного backup.sh. РАЗРУШИТЕЛЬНО: текущие данные БД и storage заменяются.
# Использование: deploy/restore.sh <каталог_бэкапа> --confirm   (запускать из корня репозитория)
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

echo "[restore] остановка приложения (БД остаётся запущенной)"
docker compose stop frontend backend worker
docker compose up -d db
docker compose exec -T db sh -c 'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"; do sleep 1; done'

echo "[restore] PostgreSQL"
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --single-transaction' < "${SRC}/db.dump"

echo "[restore] storage"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${PROJECT}_storage:/data" \
  -v "${SRC_ABS}:/backup:ro" \
  alpine:3 sh -c 'find /data -mindepth 1 -delete && tar -C /data -xzf /backup/storage.tar.gz'

echo "[restore] миграции и запуск"
docker compose run --rm migrate
docker compose up -d
echo "[restore] готово. Проверьте: curl -s http://127.0.0.1:\${HTTP_PORT:-8080}/api/health/ready"
