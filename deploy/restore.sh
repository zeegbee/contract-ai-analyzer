#!/usr/bin/env bash
# Восстановление из бэкапа. РАЗРУШИТЕЛЬНО: текущие данные БД и storage заменяются.
# Использование (из корня репозитория):
#   deploy/restore.sh <каталог_бэкапа> --confirm                                  незашифрованный бэкап
#   deploy/restore.sh <файл.tar.age> --identity <ключ_age.txt> --confirm           зашифрованный (этап 08A)
# Зашифрованную копию из внешнего хранилища сначала скачайте: deploy/backup.sh --fetch <имя>.
# Приватный ключ age хранится у администратора вне сервера; после восстановления удалите его с сервера.
# Восстанавливайте бэкап той же или более ранней версией приложения, чем текущая: после восстановления
# migrate доводит схему до текущей; бэкап из более новой версии (неизвестная ревизия) не восстанавливается.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=deploy/lib/backup-common.sh
source "${ROOT}/deploy/lib/backup-common.sh"

SRC=""
IDENTITY=""
CONFIRM=0
while (($#)); do
  case "$1" in
    --confirm) CONFIRM=1 ;;
    --identity) IDENTITY="${2:-}"; shift ;;
    -*) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
    *) SRC="$1" ;;
  esac
  shift
done
if [[ -z "${SRC}" || ${CONFIRM} -ne 1 ]]; then
  echo "Использование: $0 <каталог_бэкапа | файл.tar.age --identity <ключ>> --confirm" >&2
  echo "Восстановление заменит текущие данные. Сначала сделайте свежий бэкап." >&2
  exit 2
fi

TMP=""
cleanup() {
  [[ -n "${TMP}" && -d "${TMP}" ]] && rm -rf "${TMP}"
  return 0
}
trap cleanup EXIT

if [[ -f "${SRC}" ]]; then
  [[ "${SRC}" == *.tar.age ]] || { echo "Файл бэкапа должен иметь расширение .tar.age" >&2; exit 2; }
  [[ -n "${IDENTITY}" && -f "${IDENTITY}" ]] || { echo "Укажите файл приватного ключа age: --identity <файл>" >&2; exit 2; }
  if [[ -f "${SRC}.sha256" ]]; then
    echo "[restore] проверка контрольной суммы архива"
    [[ "$(sha256sum < "${SRC}" | cut -d' ' -f1)" == "$(cut -d' ' -f1 < "${SRC}.sha256")" ]] \
      || { echo "[restore] контрольная сумма архива не совпадает" >&2; exit 1; }
  fi
  ensure_tools_image
  TMP="$(mktemp -d "${ROOT}/.restore-XXXXXX")"
  IDENTITY_ABS="$(abs_dir "$(dirname "${IDENTITY}")")/$(basename "${IDENTITY}")"
  echo "[restore] расшифровка"
  tools -i -v "${IDENTITY_ABS}:/key:ro" "${TOOLS_IMAGE}" age -d -i /key < "${SRC}" | tar -C "${TMP}" -xf -
  inner="$(backup_names "${TMP}" | grep -v '\.tar\.age$' | head -1)"
  [[ -n "${inner}" ]] || { echo "[restore] в архиве нет каталога бэкапа" >&2; exit 1; }
  SRC="${TMP}/${inner}"
fi

cd "${ROOT}"
PROJECT="${COMPOSE_PROJECT_NAME:-contract-ai-analyzer}"
SRC_ABS="$(cd "${SRC}" && pwd)"

echo "[restore] проверка контрольных сумм"
( cd "${SRC_ABS}" && sha256sum -c SHA256SUMS )

REVISION=""
if [[ -f "${SRC_ABS}/MANIFEST" ]]; then
  REVISION="$(grep -E '^alembic_revision=' "${SRC_ABS}/MANIFEST" | cut -d= -f2-)"
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
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --single-transaction --exit-on-error' < "${SRC_ABS}/db.dump"

echo "[restore] storage"
ensure_tools_image
# Volume storage принадлежит пользователю приложения (uid 10001) — распаковываем им, архив подаётся через stdin.
MSYS_NO_PATHCONV=1 docker run --rm -i -v "${PROJECT}_storage:/data" "${TOOLS_IMAGE}" \
  sh -c 'find /data -mindepth 1 -delete && tar -C /data -xzf -' < "${SRC_ABS}/storage.tar.gz"

echo "[restore] миграции и запуск"
docker compose run --rm migrate
docker compose up -d
echo "[restore] готово. Проверьте: curl -s http://127.0.0.1:\${HTTP_PORT:-8080}/api/health/ready"
