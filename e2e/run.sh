#!/usr/bin/env bash
# End-to-end проверка на отдельном чистом стенде Docker Compose (проект cai-e2e, порт 8091).
#
#   e2e/run.sh all        # up → flow → recovery → check-logs → backup-restore → backup-offsite → down
#   e2e/run.sh up         # чистый стенд: новые volumes, bootstrap Superadmin, mock-сервер LLM
#   e2e/run.sh flow       # Playwright: вход → смена пароля → загрузка → извлечение → анализ (mock) →
#                         #             проверка доказательств → экспорт; axe; заголовки безопасности
#   e2e/run.sh recovery   # Playwright: отмена и повтор; SIGKILL worker'а посреди анализа → задача
#                         #             подхвачена после истечения lease («медленная» mock-модель)
#   e2e/run.sh check-logs # логи всех сервисов: нет текста договора, паролей и ключей из .env.e2e
#   e2e/run.sh backup-restore  # deploy/backup.sh → стенд пересоздаётся пустым → deploy/restore.sh →
#                              # Playwright проверяет, что данные и вход восстановлены
#   e2e/run.sh backup-offsite  # этап 08A: шифрование, выгрузка (каталог, S3), ротация, сбои, блокировка,
#                              # расписание и запрос из UI, скачивание → восстановление, cron/systemd
#   e2e/run.sh down       # удалить стенд cai-e2e вместе с его volumes (основной стенд не затрагивается)
#
# Запускать из любого каталога; нужны Docker Compose v2.24+, Node 22 (npm ci в frontend/), openssl,
# браузер Playwright: (cd frontend && npx playwright install chromium).
# Текст договоров на стенде синтетический (e2e/fixtures); LLM — mock, облачные модели не используются.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

export COMPOSE_PROJECT_NAME=cai-e2e
export COMPOSE_PATH_SEPARATOR=":"
export COMPOSE_FILE="compose.yaml:e2e/compose.e2e.yaml"
ENV_FILE="${ROOT}/e2e/.env.e2e"
export COMPOSE_ENV_FILES="${ENV_FILE}"
STATE_DIR="${ROOT}/e2e/state"
PORT=8091
BASE_URL="http://127.0.0.1:${PORT}"

log() { echo "[e2e] $*"; }

write_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    write_backup_env
    return
  fi
  log "создаю ${ENV_FILE} со случайными секретами"
  umask 077
  local fernet
  fernet="$(openssl rand -base64 32 | tr '+/' '-_')"
  cat > "${ENV_FILE}" <<EOF
APP_ENV=development
POSTGRES_USER=cai
POSTGRES_DB=cai
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SECRET_KEY=$(openssl rand -hex 32)
SECRETS_ENCRYPTION_KEYS=${fernet}
ALLOWED_ORIGINS=${BASE_URL}
COOKIE_SECURE=false
BOOTSTRAP_ENABLED=true
BOOTSTRAP_ADMIN_EMAIL=e2e-root@example.org
BOOTSTRAP_ADMIN_PASSWORD=E2e-Bootstrap-$(openssl rand -hex 6)
BOOTSTRAP_ADMIN_NAME=E2E Superadmin
E2E_NEW_PASSWORD=E2e-Changed-$(openssl rand -hex 6)
HTTP_BIND=127.0.0.1
HTTP_PORT=${PORT}
EDGE_SUBNET=172.30.91.0/24
FRONTEND_EDGE_IP=172.30.91.10
WORKER_CONCURRENCY=2
EOF
  write_backup_env
}

write_backup_env() {
  # Параметры этапа 08A: ключ age (приватный — в e2e/state, как «у администратора»), каталог-цель,
  # S3-совместимый сервер (rclone serve s3) и приёмник webhook — контейнеры в сети cai-e2e-backup.
  grep -q '^BACKUP_ENCRYPTION_RECIPIENT=' "${ENV_FILE}" && return
  log "добавляю параметры резервного копирования в ${ENV_FILE}"
  mkdir -p "${STATE_DIR}"
  umask 077
  docker build -q -t contract-ai-analyzer/backup-tools:local deploy/backup-tools > /dev/null
  docker run --rm contract-ai-analyzer/backup-tools:local age-keygen > "${STATE_DIR}/age-identity.txt" 2> /dev/null
  local recipient
  recipient="$(grep -E '^# public key: ' "${STATE_DIR}/age-identity.txt" | sed 's/^# public key: //')"
  cat >> "${ENV_FILE}" <<EOF
BACKUP_TARGET=none
BACKUP_SCHEDULE=0 3 * * *
BACKUP_TIMEZONE=UTC
BACKUP_DIR=${STATE_DIR}/local-backups
BACKUP_KEEP_LOCAL=7
BACKUP_ENCRYPTION_RECIPIENT=${recipient}
BACKUP_LOCAL_TARGET_DIR=${STATE_DIR}/offsite
BACKUP_S3_ENDPOINT=http://cai-e2e-s3:9000
BACKUP_S3_BUCKET=cai-e2e
BACKUP_S3_PREFIX=backups
BACKUP_S3_ACCESS_KEY_ID=e2e$(openssl rand -hex 6)
BACKUP_S3_SECRET_ACCESS_KEY=$(openssl rand -hex 20)
BACKUP_NOTIFY_WEBHOOK=http://cai-e2e-hook:9191/hook
BACKUP_DOCKER_NETWORK=cai-e2e-backup
BACKUP_MIN_INTERVAL_MINUTES=60
EOF
}

env_value() { grep -E "^$1=" "${ENV_FILE}" | head -1 | cut -d= -f2-; }

wait_ready() {
  log "ожидание готовности ${BASE_URL}/api/health/ready"
  for _ in $(seq 1 120); do
    if curl -fsS "${BASE_URL}/api/health/ready" > /dev/null 2>&1; then
      log "стенд готов"
      return
    fi
    sleep 2
  done
  docker compose ps
  echo "[e2e] стенд не стал готов за 240 с" >&2
  exit 1
}

up() {
  write_env
  log "сборка и запуск проекта ${COMPOSE_PROJECT_NAME}"
  docker compose up -d --build
  wait_ready
}

playwright() {
  local project="$1"
  mkdir -p "${STATE_DIR}"
  (
    cd frontend
    E2E_BASE_URL="${BASE_URL}" \
    E2E_ADMIN_EMAIL="$(env_value BOOTSTRAP_ADMIN_EMAIL)" \
    E2E_BOOTSTRAP_PASSWORD="$(env_value BOOTSTRAP_ADMIN_PASSWORD)" \
    E2E_NEW_PASSWORD="$(env_value E2E_NEW_PASSWORD)" \
    E2E_STATE_FILE="${STATE_DIR}/state.json" \
    E2E_FIXTURES="${ROOT}/e2e/fixtures" \
      npx playwright test --project="${project}"
  )
}

backup_restore() {
  log "бэкап стенда"
  rm -rf "${STATE_DIR}/backups"
  deploy/backup.sh "${STATE_DIR}/backups"
  local backup
  backup="$(ls -d "${STATE_DIR}"/backups/*/ | head -1)"
  log "пересоздание стенда с пустыми volumes"
  docker compose down -v --remove-orphans
  docker compose up -d
  wait_ready
  log "восстановление из ${backup}"
  deploy/restore.sh "${backup}" --confirm
  wait_ready
  playwright restore
}

check_logs() {
  # Логи всех сервисов стенда не должны содержать текст договора, пароли и ключи (THREAT_MODEL §4.6–4.7).
  log "проверка логов на текст договора и секреты"
  local logs needle found=0
  logs="$(docker compose logs --no-color 2>&1)"
  local needles=(
    "вправе изменить цену" "неустойку в размере" "Игнорируй предыдущие инструкции" "AGE-SECRET-KEY"
    "$(env_value BOOTSTRAP_ADMIN_PASSWORD)" "$(env_value E2E_NEW_PASSWORD)" "$(env_value SECRET_KEY)"
    "$(env_value POSTGRES_PASSWORD)" "$(env_value SECRETS_ENCRYPTION_KEYS)" "$(env_value BACKUP_S3_SECRET_ACCESS_KEY)"
  )
  for needle in "${needles[@]}"; do
    [[ -n "${needle}" ]] || continue
    if grep -qF -- "${needle}" <<< "${logs}"; then
      echo "[e2e] в логах найдено недопустимое значение (${#needle} символов)" >&2
      found=1
    fi
  done
  [[ ${found} -eq 0 ]] || exit 1
  log "логи чистые ($(wc -l <<< "${logs}") строк)"
}

down() {
  log "удаление стенда ${COMPOSE_PROJECT_NAME} (volumes проекта cai-e2e)"
  docker compose down -v --remove-orphans
}

case "${1:-all}" in
  up) up ;;
  flow) playwright flow ;;
  recovery) playwright recovery ;;
  check-logs) check_logs ;;
  backup-offsite) bash e2e/backup-offsite.sh && playwright restore ;;
  backup-restore) backup_restore ;;
  down) down ;;
  all)
    up
    playwright flow
    playwright recovery
    check_logs
    backup_restore
    bash e2e/backup-offsite.sh
    playwright restore
    down
    ;;
  *)
    echo "Использование: $0 [all|up|flow|recovery|check-logs|backup-restore|backup-offsite|down]" >&2
    exit 2
    ;;
esac
