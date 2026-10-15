#!/usr/bin/env bash
# Резервная копия: PostgreSQL (pg_dump -Fc) + файловое хранилище (volume storage).
# Использование: deploy/backup.sh [каталог_бэкапов]   (запускать из корня репозитория)
# Порядок: сначала БД, затем файлы — файл без ссылки в БД допустим, ссылка без файла — нет.
# Бэкап содержит конфиденциальные договоры: шифруйте его (age/gpg) и храните вне хоста.
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-contract-ai-analyzer}"
OUT_ROOT="${1:-./backups}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT_ROOT}/${TS}"
mkdir -p "${OUT}"
OUT_ABS="$(cd "${OUT}" && pwd)"

echo "[backup] PostgreSQL → ${OUT}/db.dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > "${OUT}/db.dump"

echo "[backup] storage → ${OUT}/storage.tar.gz"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${PROJECT}_storage:/data:ro" \
  -v "${OUT_ABS}:/backup" \
  alpine:3 tar -C /data -czf /backup/storage.tar.gz .

( cd "${OUT}" && sha256sum db.dump storage.tar.gz > SHA256SUMS )
echo "[backup] готово: ${OUT}"
