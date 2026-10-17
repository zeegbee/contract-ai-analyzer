#!/usr/bin/env bash
# Резервное копирование: PostgreSQL (pg_dump -Fc) + файловое хранилище (volume storage).
# Запускать из корня репозитория. Подробно — docs/OPERATIONS.md §5 и §16.
#
#   deploy/backup.sh                 бэкап сейчас (по настройкам: шифрование, выгрузка, ротация)
#   deploy/backup.sh --scheduled     опрос планировщиком (deploy/backup-schedule.sh): бэкап, только если
#                                    по расписанию или по запросу из UI пора, не чаще BACKUP_MIN_INTERVAL_MINUTES
#   deploy/backup.sh --check         проверить настройки и выйти
#   deploy/backup.sh --list          локальные копии и копии во внешнем хранилище
#   deploy/backup.sh --fetch <имя>   скачать копию из внешнего хранилища в BACKUP_DIR/restore/
#   deploy/backup.sh <каталог>       простой незашифрованный бэкап в каталог, без выгрузки и ротации
#                                    (режим до этапа 08A; используется в e2e)
#
# Параметры — BACKUP_* в .env (или COMPOSE_ENV_FILES) и deploy/backup.env; расписание, цель, сроки
# хранения и режим ротации могут быть переопределены в UI (их сообщает приложение: app.cli backup-plan).
# Пути, адрес и ключи хранилища, ключ шифрования, webhook берутся ТОЛЬКО из файлов на сервере.
#
# Коды выхода: 0 — успешно (или по расписанию ещё рано), 1 — ошибка бэкапа, 2 — некорректные настройки,
# 3 — уже выполняется другой бэкап (ручной запуск).
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
# shellcheck source=deploy/lib/backup-common.sh
source deploy/lib/backup-common.sh

mapfile -t ENV_FILE_LIST < <(env_files)
load_env_files "${ENV_FILE_LIST[@]}"
PROJECT="${COMPOSE_PROJECT_NAME:-contract-ai-analyzer}"

MODE="run"
LEGACY_DIR=""
FETCH_NAME=""
case "${1:-}" in
  "") ;;
  --scheduled) MODE="scheduled" ;;
  --check) MODE="check" ;;
  --list) MODE="list" ;;
  --fetch) MODE="fetch"; FETCH_NAME="${2:-}" ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  -*) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  *) MODE="legacy"; LEGACY_DIR="$1" ;;
esac

# ---------------------------------------------------------------- простой режим (каталог)

legacy_backup() {
  local out_root="$1" ts out out_abs revision
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  out="${out_root}/${ts}"
  mkdir -p "${out}"
  out_abs="$(abs_dir "${out}")"
  trap 'status=$?; if [[ ${status} -ne 0 ]]; then rm -rf "${out_abs}"; echo "[backup] ОШИБКА (код ${status}), неполный бэкап удалён" >&2; fi' EXIT
  dump_to "${out_abs}"
  ( cd "${out}" && sha256sum db.dump storage.tar.gz MANIFEST > SHA256SUMS )
  revision="$(grep -E '^alembic_revision=' "${out}/MANIFEST" | cut -d= -f2-)"
  echo "[backup] готово: ${out} (схема ${revision})"
}

# db.dump, storage.tar.gz и MANIFEST в каталог $1.
dump_to() {
  local dir="$1" revision
  echo "[backup] PostgreSQL → db.dump"
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > "${dir}/db.dump"
  revision="$(docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"' | tr -d '[:space:]')"
  echo "[backup] storage → storage.tar.gz"
  ensure_tools_image
  # Volume storage принадлежит пользователю приложения (uid 10001) — читаем им, архив пишет хост через stdout.
  MSYS_NO_PATHCONV=1 docker run --rm -v "${PROJECT}_storage:/data:ro" "${TOOLS_IMAGE}" tar -C /data -czf - . > "${dir}/storage.tar.gz"
  cat > "${dir}/MANIFEST" <<EOF
created_at=$(basename "${dir}")
project=${PROJECT}
alembic_revision=${revision}
EOF
}

if [[ "${MODE}" == "legacy" ]]; then
  legacy_backup "${LEGACY_DIR}"
  exit 0
fi

# ---------------------------------------------------------------- настройки

BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "${BACKUP_DIR}"
BACKUP_DIR_ABS="$(abs_dir "${BACKUP_DIR}")"
BACKUP_LOG_FILE="${BACKUP_DIR_ABS}/backup.log"

# Значения из приложения (UI) или, если приложение недоступно, из файлов окружения.
TARGET="${BACKUP_TARGET:-none}"
KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-7}"
KEEP_REMOTE_DAILY="${BACKUP_KEEP_REMOTE_DAILY:-7}"
KEEP_REMOTE_WEEKLY="${BACKUP_KEEP_REMOTE_WEEKLY:-4}"
KEEP_REMOTE_MONTHLY="${BACKUP_KEEP_REMOTE_MONTHLY:-6}"
REMOTE_PRUNE="${BACKUP_REMOTE_PRUNE:-}"
PLAN_STATUS=""
PLAN_DUE="0"
PLAN_TRIGGER="none"
PLAN_PROBLEMS=()

app_cli() {
  # Команда приложения: в работающем backend или (если он остановлен) в одноразовом контейнере migrate.
  if docker compose exec -T backend true > /dev/null 2>&1; then
    docker compose exec -T backend python -m app.cli "$@"
  else
    docker compose run --rm -T --no-deps migrate python -m app.cli "$@"
  fi
}

read_plan() {
  # Вывод приложения — только данные: принимаются ключи из списка, значения проверяются, ничего не исполняется.
  local output line key value
  if ! output="$(app_cli backup-plan "$@" 2> "${BACKUP_DIR_ABS}/.plan.err")"; then
    [[ -n "${output}" ]] || return 1
  fi
  while IFS= read -r line; do
    [[ "${line}" =~ ^([A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]%$'\r'}"
    case "${key}" in
      STATUS) [[ "${value}" =~ ^(ok|config_error)$ ]] && PLAN_STATUS="${value}" ;;
      DUE) [[ "${value}" =~ ^[01]$ ]] && PLAN_DUE="${value}" ;;
      TRIGGER) [[ "${value}" =~ ^(scheduled|requested|none)$ ]] && PLAN_TRIGGER="${value}" ;;
      TARGET) [[ "${value}" =~ ^(none|s3|local)$ ]] && TARGET="${value}" ;;
      KEEP_LOCAL) is_uint "${value}" && KEEP_LOCAL="${value}" ;;
      KEEP_REMOTE_DAILY) is_uint "${value}" && KEEP_REMOTE_DAILY="${value}" ;;
      KEEP_REMOTE_WEEKLY) is_uint "${value}" && KEEP_REMOTE_WEEKLY="${value}" ;;
      KEEP_REMOTE_MONTHLY) is_uint "${value}" && KEEP_REMOTE_MONTHLY="${value}" ;;
      REMOTE_PRUNE) [[ "${value}" =~ ^(script|lifecycle)$ ]] && REMOTE_PRUNE="${value}" ;;
      PROBLEM) [[ "${value}" =~ ^[a-z_]{1,64}$ ]] && PLAN_PROBLEMS+=("${value}") ;;
    esac
  done <<< "${output}"
  [[ -n "${PLAN_STATUS}" ]]
}

CONFIG_ERRORS=()
validate_config() {
  # Проверка на сервере (не полагается на приложение): цель, числа, пути, ключ, учётные данные.
  CONFIG_ERRORS=()
  [[ -n "${REMOTE_PRUNE}" ]] || REMOTE_PRUNE=$([[ "${TARGET}" == "local" ]] && echo script || echo lifecycle)
  [[ "${TARGET}" =~ ^(none|s3|local)$ ]] || CONFIG_ERRORS+=("BACKUP_TARGET: none, s3 или local")
  [[ "${REMOTE_PRUNE}" =~ ^(script|lifecycle)$ ]] || CONFIG_ERRORS+=("BACKUP_REMOTE_PRUNE: script или lifecycle")
  local name
  for name in KEEP_LOCAL KEEP_REMOTE_DAILY KEEP_REMOTE_WEEKLY KEEP_REMOTE_MONTHLY; do
    is_uint "${!name}" && ((${!name} <= 1000)) || CONFIG_ERRORS+=("BACKUP_${name}: целое число 0–1000")
  done
  is_uint "${KEEP_LOCAL}" && ((KEEP_LOCAL >= 1)) || CONFIG_ERRORS+=("BACKUP_KEEP_LOCAL: не меньше 1")
  is_uint "${BACKUP_MIN_INTERVAL_MINUTES:-60}" || CONFIG_ERRORS+=("BACKUP_MIN_INTERVAL_MINUTES: целое число минут")
  local recipient="${BACKUP_ENCRYPTION_RECIPIENT:-}"
  if [[ -n "${recipient}" && ! "${recipient}" =~ ${AGE_RECIPIENT_RE} ]]; then
    CONFIG_ERRORS+=("BACKUP_ENCRYPTION_RECIPIENT: не публичный ключ age (age1…)")
  fi
  if [[ "${TARGET}" != "none" ]]; then
    [[ -n "${recipient}" ]] || CONFIG_ERRORS+=("BACKUP_ENCRYPTION_RECIPIENT обязателен для выгрузки за пределы сервера")
    if [[ "${REMOTE_PRUNE}" == "script" ]] && is_uint "${KEEP_REMOTE_DAILY}" && is_uint "${KEEP_REMOTE_WEEKLY}" \
        && is_uint "${KEEP_REMOTE_MONTHLY}" && ((KEEP_REMOTE_DAILY + KEEP_REMOTE_WEEKLY + KEEP_REMOTE_MONTHLY == 0)); then
      CONFIG_ERRORS+=("BACKUP_KEEP_REMOTE_*: во внешнем хранилище должна храниться хотя бы одна копия")
    fi
  fi
  if [[ "${TARGET}" == "local" ]]; then
    local dir="${BACKUP_LOCAL_TARGET_DIR:-}"
    if [[ -z "${dir}" || "${dir}" != /* ]]; then
      CONFIG_ERRORS+=("BACKUP_LOCAL_TARGET_DIR: абсолютный путь к каталогу")
    elif [[ ! -d "${dir}" || ! -w "${dir}" ]]; then
      CONFIG_ERRORS+=("BACKUP_LOCAL_TARGET_DIR: каталог не существует или недоступен для записи")
    elif [[ "$(abs_dir "${dir}")" == "${BACKUP_DIR_ABS}" ]]; then
      CONFIG_ERRORS+=("BACKUP_LOCAL_TARGET_DIR должен отличаться от BACKUP_DIR")
    fi
  fi
  if [[ "${TARGET}" == "s3" ]]; then
    for name in BACKUP_S3_ENDPOINT BACKUP_S3_BUCKET BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY; do
      [[ -n "${!name:-}" ]] || CONFIG_ERRORS+=("${name} обязателен для BACKUP_TARGET=s3")
    done
    [[ "${BACKUP_S3_BUCKET:-}" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$|^$ ]] || CONFIG_ERRORS+=("BACKUP_S3_BUCKET: некорректное имя")
    [[ "${BACKUP_S3_PREFIX:-}" =~ ^[A-Za-z0-9._/-]*$ ]] || CONFIG_ERRORS+=("BACKUP_S3_PREFIX: допустимы латиница, цифры, . _ / -")
  fi
  ((${#CONFIG_ERRORS[@]} == 0))
}

# ---------------------------------------------------------------- внешнее хранилище

remote_path() {
  local prefix="${BACKUP_S3_PREFIX:-}"
  prefix="${prefix#/}"
  prefix="${prefix%/}"
  echo "backup:${BACKUP_S3_BUCKET}${prefix:+/${prefix}}"
}

rclone_run() {
  # Учётные данные — через окружение контейнера (имена переменных, без значений в аргументах команды).
  export RCLONE_CONFIG_BACKUP_TYPE=s3 RCLONE_CONFIG_BACKUP_PROVIDER=Other RCLONE_CONFIG_BACKUP_FORCE_PATH_STYLE=true
  export RCLONE_CONFIG_BACKUP_ENDPOINT="${BACKUP_S3_ENDPOINT:-}" RCLONE_CONFIG_BACKUP_REGION="${BACKUP_S3_REGION:-}"
  export RCLONE_CONFIG_BACKUP_ACCESS_KEY_ID="${BACKUP_S3_ACCESS_KEY_ID:-}"
  export RCLONE_CONFIG_BACKUP_SECRET_ACCESS_KEY="${BACKUP_S3_SECRET_ACCESS_KEY:-}"
  tools --add-host host.docker.internal:host-gateway \
    -e RCLONE_CONFIG_BACKUP_TYPE -e RCLONE_CONFIG_BACKUP_PROVIDER -e RCLONE_CONFIG_BACKUP_FORCE_PATH_STYLE \
    -e RCLONE_CONFIG_BACKUP_ENDPOINT -e RCLONE_CONFIG_BACKUP_REGION \
    -e RCLONE_CONFIG_BACKUP_ACCESS_KEY_ID -e RCLONE_CONFIG_BACKUP_SECRET_ACCESS_KEY \
    "$@"
}

remote_list() {
  case "${TARGET}" in
    local) backup_names "${BACKUP_LOCAL_TARGET_DIR}" ;;
    s3) rclone_run "${TOOLS_IMAGE}" rclone lsf --files-only "$(remote_path)/" 2> /dev/null | grep -E "${BACKUP_NAME_RE}" || true ;;
  esac
}

remote_delete() {
  local name="$1"
  case "${TARGET}" in
    local) rm -f "${BACKUP_LOCAL_TARGET_DIR}/${name}" "${BACKUP_LOCAL_TARGET_DIR}/${name}.sha256" ;;
    s3)
      rclone_run "${TOOLS_IMAGE}" rclone deletefile "$(remote_path)/${name}"
      rclone_run "${TOOLS_IMAGE}" rclone deletefile "$(remote_path)/${name}.sha256" 2> /dev/null || true
      ;;
  esac
}

upload() {
  # Выгрузка зашифрованного архива и файла контрольной суммы (ошибка любой команды прерывает бэкап).
  local name="$1"
  case "${TARGET}" in
    local)
      cp "${BACKUP_DIR_ABS}/${name}" "${BACKUP_LOCAL_TARGET_DIR}/${name}.part"
      cp "${BACKUP_DIR_ABS}/${name}.sha256" "${BACKUP_LOCAL_TARGET_DIR}/${name}.sha256"
      mv "${BACKUP_LOCAL_TARGET_DIR}/${name}.part" "${BACKUP_LOCAL_TARGET_DIR}/${name}"
      ;;
    s3)
      rclone_run -v "${BACKUP_DIR_ABS}:/backup:ro" "${TOOLS_IMAGE}" rclone copyto "/backup/${name}" "$(remote_path)/${name}"
      rclone_run -v "${BACKUP_DIR_ABS}:/backup:ro" "${TOOLS_IMAGE}" rclone copyto "/backup/${name}.sha256" "$(remote_path)/${name}.sha256"
      ;;
  esac
}

verify_remote() {
  # Контрольная сумма копии на стороне хранилища должна совпасть с локальной.
  local name="$1" file="${BACKUP_DIR_ABS}/$1" expected actual
  case "${TARGET}" in
    local)
      expected="$(cut -d' ' -f1 < "${file}.sha256")"
      actual="$(sha256sum < "${BACKUP_LOCAL_TARGET_DIR}/${name}" | cut -d' ' -f1)"
      ;;
    s3)
      expected="$(md5sum < "${file}" | cut -d' ' -f1)"
      actual="$(rclone_run "${TOOLS_IMAGE}" rclone md5sum "$(remote_path)/${name}" | cut -d' ' -f1)"
      if [[ -z "${actual}" ]]; then
        # Хранилище не сообщает MD5 — сверяем скачанную копию.
        log "хранилище не сообщает MD5 — сверка скачанной копии"
        actual="$(rclone_run "${TOOLS_IMAGE}" rclone cat "$(remote_path)/${name}" | md5sum | cut -d' ' -f1)"
      else
        log "проверка: MD5 объекта в хранилище"
      fi
      ;;
  esac
  [[ -n "${expected}" && "${expected}" == "${actual}" ]]
}

# ---------------------------------------------------------------- режимы без бэкапа

if [[ "${MODE}" == "check" || "${MODE}" == "list" || "${MODE}" == "fetch" ]]; then
  read_plan || log "приложение недоступно — используются значения из файлов окружения"
  if ! validate_config; then
    printf 'Настройки бэкапа некорректны:\n' >&2
    printf '  - %s\n' "${CONFIG_ERRORS[@]}" >&2
    exit 2
  fi
  if [[ "${PLAN_STATUS}" == "config_error" ]]; then
    echo "Приложение сообщает о проблемах настроек: ${PLAN_PROBLEMS[*]}" >&2
    cat "${BACKUP_DIR_ABS}/.plan.err" >&2 || true
    exit 2
  fi
  ensure_tools_image
  case "${MODE}" in
    check) echo "Настройки корректны: цель ${TARGET}, локальных копий ${KEEP_LOCAL}, ротация внешних копий ${REMOTE_PRUNE}" ;;
    list)
      echo "Локальные копии (${BACKUP_DIR_ABS}):"
      backup_names "${BACKUP_DIR_ABS}" | sort -r | sed 's/^/  /'
      if [[ "${TARGET}" != "none" ]]; then
        echo "Внешнее хранилище (${TARGET}):"
        remote_list | sort -r | sed 's/^/  /'
      fi
      ;;
    fetch)
      [[ "${FETCH_NAME}" =~ ${BACKUP_NAME_RE} && "${FETCH_NAME}" == *.tar.age ]] || { echo "Укажите имя вида 20261017T030000Z.tar.age" >&2; exit 2; }
      [[ "${TARGET}" != "none" ]] || { echo "Внешнее хранилище не настроено (BACKUP_TARGET=none)" >&2; exit 2; }
      mkdir -p "${BACKUP_DIR_ABS}/restore"
      dest="${BACKUP_DIR_ABS}/restore/${FETCH_NAME}"
      case "${TARGET}" in
        local) cp "${BACKUP_LOCAL_TARGET_DIR}/${FETCH_NAME}" "${dest}"; cp "${BACKUP_LOCAL_TARGET_DIR}/${FETCH_NAME}.sha256" "${dest}.sha256" ;;
        s3)
          rclone_run "${TOOLS_IMAGE}" rclone cat "$(remote_path)/${FETCH_NAME}" > "${dest}"
          rclone_run "${TOOLS_IMAGE}" rclone cat "$(remote_path)/${FETCH_NAME}.sha256" > "${dest}.sha256"
          ;;
      esac
      [[ "$(sha256sum < "${dest}" | cut -d' ' -f1)" == "$(cut -d' ' -f1 < "${dest}.sha256")" ]] \
        || { echo "Контрольная сумма скачанной копии не совпадает" >&2; exit 1; }
      echo "Скачано и проверено: ${dest}"
      echo "Восстановление: deploy/restore.sh ${dest} --identity <файл приватного ключа age> --confirm"
      ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- бэкап

if ! acquire_lock "${BACKUP_DIR_ABS}/.lock"; then
  if [[ "${MODE}" == "scheduled" ]]; then
    exit 0
  fi
  err "уже выполняется другой бэкап"
  exit 3
fi

notify_daily() {
  # Повторяющуюся при опросе каждые 5 минут ошибку сообщаем не чаще раза в сутки.
  local marker="${BACKUP_DIR_ABS}/.notified-$1"
  if [[ ! -f "${marker}" || -n "$(find "${marker}" -mmin +1440 2> /dev/null)" ]]; then
    notify_failure "$1"
    touch "${marker}"
  fi
}

RUN_ID=""
STAGE="plan"
WORK=""
NAME=""
SIZE=""
ENCRYPTED=0
UPLOADED=0
VERIFIED=0
PRUNED_LOCAL=0
PRUNED_REMOTE=0
TRIGGER="manual"

report() {
  [[ -n "${RUN_ID}" ]] || return 0
  app_cli backup-report "$@" --run-id "${RUN_ID}" > /dev/null 2>&1 || log "не удалось сообщить приложению о результате"
}

finish() {
  local status=$?
  [[ -n "${WORK}" && -d "${WORK}" ]] && rm -rf "${WORK}"
  if [[ ${status} -ne 0 && -n "${RUN_ID}" ]]; then
    local code="${STAGE}_failed"
    err "бэкап не выполнен на этапе ${STAGE}"
    [[ -n "${NAME}" && "${STAGE}" == "encrypt" ]] && rm -f "${BACKUP_DIR_ABS:?}/${NAME}" "${BACKUP_DIR_ABS:?}/${NAME}.sha256"
    report finish --status failed --error-code "${code}" --encrypted "${ENCRYPTED}" --uploaded "${UPLOADED}" --verified "${VERIFIED}"
    notify_failure "${code}"
  fi
  release_lock
  exit "${status}"
}
trap finish EXIT

ensure_tools_image
PLAN_ARGS=()
[[ "${MODE}" == "scheduled" ]] && PLAN_ARGS=(--scheduler)
if read_plan "${PLAN_ARGS[@]}"; then
  :
elif [[ "${MODE}" == "scheduled" ]]; then
  err "приложение недоступно — не удалось получить настройки и расписание"
  notify_daily "app_unavailable"
  exit 1
else
  log "приложение недоступно — используются значения из файлов окружения"
fi

if [[ "${MODE}" == "scheduled" ]]; then
  [[ "${PLAN_STATUS}" == "ok" && "${PLAN_DUE}" == "1" ]] || {
    if [[ "${PLAN_STATUS}" == "config_error" ]]; then
      err "настройки бэкапа некорректны (приложение): ${PLAN_PROBLEMS[*]}"
      notify_daily "config_invalid"
      exit 2
    fi
    exit 0
  }
  min_interval="${BACKUP_MIN_INTERVAL_MINUTES:-60}"
  if is_uint "${min_interval}" && [[ -f "${BACKUP_DIR_ABS}/.last-run" ]]; then
    last="$(cat "${BACKUP_DIR_ABS}/.last-run")"
    if is_uint "${last}" && (($(date +%s) - last < min_interval * 60)); then
      log "пропуск: предыдущий бэкап меньше ${min_interval} мин назад (BACKUP_MIN_INTERVAL_MINUTES)"
      exit 0
    fi
  fi
  TRIGGER="${PLAN_TRIGGER}"
elif [[ "${PLAN_STATUS}" == "config_error" ]]; then
  err "настройки бэкапа некорректны (приложение): ${PLAN_PROBLEMS[*]}"
  cat "${BACKUP_DIR_ABS}/.plan.err" >&2 || true
  exit 2
fi

if ! validate_config; then
  err "настройки бэкапа некорректны — бэкап не начат:"
  for problem in "${CONFIG_ERRORS[@]}"; do err "  ${problem}"; done
  [[ "${MODE}" == "scheduled" ]] && notify_daily "config_invalid"
  exit 2
fi

RUN_ID="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n' | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/')"
report start --trigger "${TRIGGER}" --target "${TARGET}"
log "бэкап начат: запуск ${TRIGGER}, цель ${TARGET}"
date +%s > "${BACKUP_DIR_ABS}/.last-run"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="${BACKUP_DIR_ABS}/.work-${TS}"
mkdir -p "${WORK}/${TS}"
STAGE="dump"
dump_to "${WORK}/${TS}" > /dev/null
( cd "${WORK}/${TS}" && sha256sum db.dump storage.tar.gz MANIFEST > SHA256SUMS )

STAGE="encrypt"
if [[ -n "${BACKUP_ENCRYPTION_RECIPIENT:-}" ]]; then
  NAME="${TS}.tar.age"
  export BACKUP_ENCRYPTION_RECIPIENT
  tar -C "${WORK}" -cf - "${TS}" \
    | docker run --rm -i -e BACKUP_ENCRYPTION_RECIPIENT "${TOOLS_IMAGE}" sh -c 'age -r "$BACKUP_ENCRYPTION_RECIPIENT"' \
    > "${BACKUP_DIR_ABS}/${NAME}"
  [[ "$(head -c 21 "${BACKUP_DIR_ABS}/${NAME}")" == "age-encryption.org/v1" ]]
  ( cd "${BACKUP_DIR_ABS}" && sha256sum "${NAME}" > "${NAME}.sha256" )
  ENCRYPTED=1
  SIZE="$(wc -c < "${BACKUP_DIR_ABS}/${NAME}" | tr -d ' ')"
else
  NAME="${TS}"
  mv "${WORK}/${TS}" "${BACKUP_DIR_ABS}/${NAME}"
  SIZE="$(du -sb "${BACKUP_DIR_ABS}/${NAME}" | cut -f1)"
fi
rm -rf "${WORK}"
WORK=""
log "архив ${NAME} (${SIZE} байт, шифрование: $([[ ${ENCRYPTED} == 1 ]] && echo age || echo нет))"

if [[ "${TARGET}" != "none" ]]; then
  STAGE="upload"
  upload "${NAME}"
  UPLOADED=1
  STAGE="verify"
  verify_remote "${NAME}"
  VERIFIED=1
  log "выгружено в ${TARGET} и проверено"
fi

# Ротация — только после успешного бэкапа и выгрузки; самый новый бэкап не удаляется никогда.
STAGE="prune"
while IFS= read -r old; do
  [[ -n "${old}" && "${old}" != "${NAME}" ]] || continue
  rm -rf "${BACKUP_DIR_ABS:?}/${old}" "${BACKUP_DIR_ABS:?}/${old}.sha256"
  PRUNED_LOCAL=$((PRUNED_LOCAL + 1))
done < <(backup_names "${BACKUP_DIR_ABS}" | keep_newest_delete "${KEEP_LOCAL}")
if [[ "${TARGET}" != "none" && "${REMOTE_PRUNE}" == "script" ]]; then
  remote_names="$(remote_list)"
  keep_names="$(gfs_keep "${KEEP_REMOTE_DAILY}" "${KEEP_REMOTE_WEEKLY}" "${KEEP_REMOTE_MONTHLY}" <<< "${remote_names}")"
  while IFS= read -r old; do
    [[ -n "${old}" && "${old}" != "${NAME}" ]] || continue
    grep -qxF "${old}" <<< "${keep_names}" && continue
    remote_delete "${old}"
    PRUNED_REMOTE=$((PRUNED_REMOTE + 1))
  done <<< "${remote_names}"
fi

STAGE="report"
report finish --status success --name "${NAME}" --size "${SIZE}" --encrypted "${ENCRYPTED}" \
  --uploaded "$([[ "${TARGET}" != "none" ]] && echo "${UPLOADED}" || echo 0)" --verified "${VERIFIED}" \
  --pruned-local "${PRUNED_LOCAL}" --pruned-remote "${PRUNED_REMOTE}"
rm -f "${BACKUP_DIR_ABS}"/.notified-*
log "бэкап завершён: ${NAME}; удалено старых копий: локально ${PRUNED_LOCAL}, во внешнем хранилище ${PRUNED_REMOTE}"
RUN_ID=""
