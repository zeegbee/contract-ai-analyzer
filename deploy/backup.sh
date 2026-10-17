#!/usr/bin/env bash
# Резервная копия: PostgreSQL (pg_dump -Fc) + файловое хранилище (volume storage).
# Использование: deploy/backup.sh [каталог_бэкапов]   (запускать из корня репозитория)
# Порядок: сначала БД, затем файлы — файл без ссылки в БД допустим, ссылка без файла — нет.
# Бэкап содержит конфиденциальные договоры: шифруйте его (age/gpg) и храните вне хоста.
set -euo pipefail
# Файлы бэкапа доступны только владельцу (на Linux по умолчанию они были бы читаемы всеми).
umask 077

PROJECT="${COMPOSE_PROJECT_NAME:-contract-ai-analyzer}"
OUT_ROOT="${1:-./backups}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT_ROOT}/${TS}"
mkdir -p "${OUT}"
OUT_ABS="$(cd "${OUT}" && pwd)"
# При ошибке не оставляем неполный бэкап, который можно принять за рабочий.
trap 'status=$?; if [[ ${status} -ne 0 ]]; then rm -rf "${OUT_ABS}"; echo "[backup] ОШИБКА (код ${status}), неполный бэкап удалён" >&2; fi' EXIT

echo "[backup] PostgreSQL → ${OUT}/db.dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > "${OUT}/db.dump"
REVISION="$(docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"' | tr -d '[:space:]')"

echo "[backup] storage → ${OUT}/storage.tar.gz"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${PROJECT}_storage:/data:ro" \
  -v "${OUT_ABS}:/backup" \
  alpine:3 tar -C /data -czf /backup/storage.tar.gz .

cat > "${OUT}/MANIFEST" <<EOF
created_at=${TS}
project=${PROJECT}
alembic_revision=${REVISION}
EOF
( cd "${OUT}" && sha256sum db.dump storage.tar.gz MANIFEST > SHA256SUMS )
echo "[backup] готово: ${OUT} (схема ${REVISION})"
