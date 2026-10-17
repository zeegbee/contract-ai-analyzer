# shellcheck shell=bash
# Общие функции deploy/backup.sh, deploy/restore.sh, deploy/backup-schedule.sh (подключается через source).
# Требования к хосту: bash 4+, Docker Compose v2, GNU coreutils (date -d, sha256sum, md5sum), tar.
# age и rclone на хост не ставятся — они в образе contract-ai-analyzer/backup-tools:local (deploy/backup-tools).

# shellcheck disable=SC2034 # константы используются в подключающих скриптах
TOOLS_IMAGE="contract-ai-analyzer/backup-tools:local"
BACKUP_NAME_RE='^[0-9]{8}T[0-9]{6}Z(\.tar\.age)?$'
AGE_RECIPIENT_RE='^age1[02-9ac-hj-np-z]{58}$'

# Переменные из файлов окружения (.env и deploy/backup.env). Файлы НЕ исполняются как shell-код:
# берутся только строки KEY=VALUE для BACKUP_* и COMPOSE_PROJECT_NAME. Уже заданные в окружении
# переменные не перезаписываются (окружение важнее файла).
load_env_files() {
  local file line key value
  for file in "$@"; do
    [[ -f "${file}" ]] || continue
    while IFS= read -r line || [[ -n "${line}" ]]; do
      line="${line%$'\r'}"
      [[ "${line}" =~ ^[[:space:]]*(BACKUP_[A-Z0-9_]+|COMPOSE_PROJECT_NAME)=(.*)$ ]] || continue
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      if [[ "${value}" =~ ^\"(.*)\"$ || "${value}" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      [[ -n "${!key+x}" ]] && continue
      printf -v "${key}" '%s' "${value}"
      export "${key?}"
    done < "${file}"
  done
}

env_files() {
  # Те же файлы, что использует docker compose: COMPOSE_ENV_FILES (через запятую) или .env.
  local files=()
  if [[ -n "${COMPOSE_ENV_FILES:-}" ]]; then
    IFS=',' read -r -a files <<< "${COMPOSE_ENV_FILES}"
  else
    files=(".env")
  fi
  printf '%s\n' "${files[@]}" "${BACKUP_ENV_FILE:-deploy/backup.env}"
}

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [backup] $*"
  echo "${line}"
  if [[ -n "${BACKUP_LOG_FILE:-}" ]]; then
    echo "${line}" >> "${BACKUP_LOG_FILE}" 2>/dev/null || true
  fi
}

err() {
  log "ОШИБКА: $*" >&2
}

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

# Имена бэкапов в каталоге (без ls | grep: посторонние файлы и необычные имена пропускаются).
backup_names() {
  local path name
  for path in "$1"/*; do
    name="${path##*/}"
    [[ "${name}" =~ ${BACKUP_NAME_RE} ]] && echo "${name}"
  done
  return 0
}

abs_dir() {
  # Абсолютный путь существующего каталога.
  (cd "$1" 2>/dev/null && pwd)
}

ensure_tools_image() {
  if ! docker image inspect "${TOOLS_IMAGE}" > /dev/null 2>&1; then
    log "сборка образа ${TOOLS_IMAGE}"
    docker build -q -t "${TOOLS_IMAGE}" "$(dirname "${BASH_SOURCE[0]}")/../backup-tools" > /dev/null
  fi
}

# Контейнер инструментов с правами пользователя хоста (читает файлы бэкапа с правами 600).
tools() {
  # BACKUP_DOCKER_NETWORK — сеть Docker, если хранилище или webhook доступны только в ней (например, контейнер).
  MSYS_NO_PATHCONV=1 docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp     ${BACKUP_DOCKER_NETWORK:+--network "${BACKUP_DOCKER_NETWORK}"} "$@"
}

# ---------------------------------------------------------------- блокировка

LOCK_DIR=""
acquire_lock() {
  # flock (Linux) или mkdir с PID (если flock нет, например в Git Bash). Возврат 1 — уже занято.
  local lock="$1"
  if command -v flock > /dev/null 2>&1; then
    exec 9> "${lock}"
    flock -n 9
    return
  fi
  if mkdir "${lock}.d" 2> /dev/null; then
    echo "$$" > "${lock}.d/pid"
    LOCK_DIR="${lock}.d"
    return 0
  fi
  local pid
  pid="$(cat "${lock}.d/pid" 2> /dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2> /dev/null; then
    return 1
  fi
  # Блокировка осталась от прерванного запуска.
  rm -rf "${lock}.d"
  mkdir "${lock}.d" && echo "$$" > "${lock}.d/pid" && LOCK_DIR="${lock}.d"
}

release_lock() {
  if [[ -n "${LOCK_DIR}" ]]; then
    rm -rf "${LOCK_DIR}"
    LOCK_DIR=""
  fi
}

# ---------------------------------------------------------------- ротация

# Имена бэкапов (по одному в строке) → имена, которые нужно СОХРАНИТЬ по схеме
# «последний бэкап каждого из N последних дней / W недель (ISO) / M месяцев». Время — UTC из имени.
# Самый новый бэкап сохраняется всегда.
gfs_keep() {
  local daily="$1" weekly="$2" monthly="$3"
  local names=() name stamp day week month
  mapfile -t names < <(grep -E "${BACKUP_NAME_RE}" | sort -r)
  ((${#names[@]} == 0)) && return 0
  declare -A seen_day=() seen_week=() seen_month=() kept=()
  kept["${names[0]}"]=1
  for name in "${names[@]}"; do
    stamp="${name:0:8}"
    day="${stamp}"
    month="${stamp:0:6}"
    week="$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2}" +%G-W%V)"
    if [[ -z "${seen_day[${day}]:-}" ]] && ((${#seen_day[@]} < daily)); then
      seen_day["${day}"]=1
      kept["${name}"]=1
    fi
    if [[ -z "${seen_week[${week}]:-}" ]] && ((${#seen_week[@]} < weekly)); then
      seen_week["${week}"]=1
      kept["${name}"]=1
    fi
    if [[ -z "${seen_month[${month}]:-}" ]] && ((${#seen_month[@]} < monthly)); then
      seen_month["${month}"]=1
      kept["${name}"]=1
    fi
  done
  printf '%s\n' "${!kept[@]}" | sort -r
}

# Имена бэкапов → имена, которые нужно УДАЛИТЬ, чтобы осталось N самых новых (N ≥ 1).
keep_newest_delete() {
  local count="$1"
  ((count < 1)) && count=1
  grep -E "${BACKUP_NAME_RE}" | sort -r | tail -n "+$((count + 1))"
}

# ---------------------------------------------------------------- уведомление

notify_failure() {
  # POST JSON на BACKUP_NOTIFY_WEBHOOK. URL передаётся контейнеру через окружение (не в аргументах),
  # тело — только код ошибки, проект и время. Ошибка уведомления не меняет результат бэкапа.
  local code="$1"
  [[ -n "${BACKUP_NOTIFY_WEBHOOK:-}" ]] || return 0
  local body
  body="{\"event\":\"backup_failed\",\"project\":\"${PROJECT}\",\"error_code\":\"${code}\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
  if tools --add-host host.docker.internal:host-gateway -e BACKUP_NOTIFY_WEBHOOK -e BODY="${body}" \
      "${TOOLS_IMAGE}" sh -c 'curl -fsS -m 20 -X POST -H "Content-Type: application/json" -d "$BODY" "$BACKUP_NOTIFY_WEBHOOK" > /dev/null'; then
    log "уведомление об ошибке отправлено"
  else
    log "не удалось отправить уведомление об ошибке"
  fi
}
