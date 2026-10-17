#!/usr/bin/env bash
# Этап 08A на e2e-стенде cai-e2e (запускается из e2e/run.sh после flow; данные стенда — синтетические).
# S3-совместимое хранилище — `rclone serve s3` в контейнере (образы MinIO больше не публикуются),
# приёмник webhook — контейнер с python; оба в сети cai-e2e-backup (BACKUP_DOCKER_NETWORK).
# В конце стенд восстанавливается из зашифрованной копии, скачанной из S3 (проверку данных
# выполняет Playwright: e2e/run.sh → playwright restore).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"
export COMPOSE_PROJECT_NAME=cai-e2e COMPOSE_PATH_SEPARATOR=":" COMPOSE_FILE="compose.yaml:e2e/compose.e2e.yaml"
ENV_FILE="${ROOT}/e2e/.env.e2e"
export COMPOSE_ENV_FILES="${ENV_FILE}"
STATE="${ROOT}/e2e/state"
BASE_URL="http://127.0.0.1:8091"
NET="cai-e2e-backup"
TOOLS="contract-ai-analyzer/backup-tools:local"
JAR="${STATE}/backup-test.cookies"

env_value() { grep -E "^$1=" "${ENV_FILE}" | head -1 | cut -d= -f2-; }
BACKUP_DIR="$(env_value BACKUP_DIR)"
OFFSITE="$(env_value BACKUP_LOCAL_TARGET_DIR)"
IDENTITY="${STATE}/age-identity.txt"
S3_KEY="$(env_value BACKUP_S3_ACCESS_KEY_ID)"
S3_SECRET="$(env_value BACKUP_S3_SECRET_ACCESS_KEY)"

# shellcheck source=deploy/lib/backup-common.sh
source deploy/lib/backup-common.sh
STEP=0
ok() { STEP=$((STEP + 1)); echo "[backup-test] ✓ ${STEP}. $*"; }
fail() { echo "[backup-test] ✗ $*" >&2; exit 1; }
expect_code() {
  # expect_code <код> <описание> <команда…>
  local want="$1" what="$2" got=0
  shift 2
  "$@" > "${STATE}/last.out" 2>&1 || got=$?
  [[ "${got}" == "${want}" ]] || { cat "${STATE}/last.out" >&2; fail "${what}: код ${got}, ожидался ${want}"; }
}
backup() { deploy/backup.sh "$@"; }
local_names() { backup_names "${BACKUP_DIR}" | sort; }
offsite_names() { backup_names "${OFFSITE}" | grep '\.tar\.age$' | sort || true; }
s3_names() {
  docker exec cai-e2e-s3 sh -c 'ls -1 /data/cai-e2e/backups 2> /dev/null' | grep -E '^[0-9]{8}T[0-9]{6}Z\.tar\.age$' | sort || true
}
count() { grep -c . <<< "$1" || true; }

# ---------------------------------------------------------------- API (как в UI)
csrf() { awk '$6 == "cai_csrf" { print $7 }' "${JAR}"; }
api() {
  local method="$1" path="$2" body="${3:-}"
  curl -fsS -b "${JAR}" -c "${JAR}" -X "${method}" -H "Origin: ${BASE_URL}" -H "X-CSRF-Token: $(csrf)" \
    -H "Content-Type: application/json" ${body:+-d "${body}"} "${BASE_URL}/api${path}"
}
login() {
  rm -f "${JAR}"
  curl -fsS -c "${JAR}" -H "Origin: ${BASE_URL}" -H "Content-Type: application/json" \
    -d "{\"email\":\"$(env_value BOOTSTRAP_ADMIN_EMAIL)\",\"password\":\"$(env_value E2E_NEW_PASSWORD)\"}" \
    "${BASE_URL}/api/auth/login" > /dev/null
}
json() { "${PY}" -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1"; }
# Первый работающий интерпретатор (на Windows python3 может быть заглушкой Microsoft Store).
PY=""
for candidate in backend/.venv/Scripts/python backend/.venv/bin/python python3 python; do
  if "${candidate}" -c "pass" > /dev/null 2>&1; then PY="${candidate}"; break; fi
done
[[ -n "${PY}" ]] || { echo "[backup-test] нужен Python 3" >&2; exit 1; }

cleanup() {
  docker rm -f cai-e2e-s3 cai-e2e-hook > /dev/null 2>&1 || true
  docker network rm "${NET}" > /dev/null 2>&1 || true
  rm -f "${JAR}"
}
trap cleanup EXIT

# ---------------------------------------------------------------- окружение теста
cleanup
rm -rf "${BACKUP_DIR}" "${OFFSITE}"
mkdir -p "${BACKUP_DIR}" "${OFFSITE}"
docker network create "${NET}" > /dev/null
docker run -d --name cai-e2e-s3 --network "${NET}" --tmpfs /data:rw,mode=1777 "${TOOLS}" \
  sh -c "mkdir -p /data/cai-e2e && exec rclone serve s3 /data --addr :9000 --auth-key '${S3_KEY},${S3_SECRET}'" > /dev/null
docker run -d --name cai-e2e-hook --network "${NET}" python:3.13-slim-trixie python -u -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        print("WEBHOOK " + body.decode(), flush=True)
        self.send_response(200); self.end_headers()
    def log_message(self, *args): pass
http.server.HTTPServer(("0.0.0.0", 9191), H).serve_forever()' > /dev/null
sleep 2
login
api PUT /admin/backup '{"reset":["schedule_enabled","schedule","timezone","target","keep_local","keep_remote_daily","keep_remote_weekly","keep_remote_monthly","remote_prune"]}' > /dev/null

# ---------------------------------------------------------------- 1. проверка настроек
expect_code 0 "--check с настройками по умолчанию" backup --check
ok "настройки по умолчанию корректны (цель none)"
expect_code 2 "неверный ключ age" env BACKUP_ENCRYPTION_RECIPIENT=age1invalid deploy/backup.sh
grep -q "не публичный ключ age" "${STATE}/last.out" || fail "нет понятного сообщения о ключе"
api PUT /admin/backup '{"target":"local"}' > /dev/null
expect_code 2 "каталог-цель не существует" env BACKUP_LOCAL_TARGET_DIR=/nonexistent/dir deploy/backup.sh
grep -q "каталог не существует" "${STATE}/last.out" || fail "нет понятного сообщения о каталоге"
expect_code 2 "каталог-цель совпадает с BACKUP_DIR" env BACKUP_LOCAL_TARGET_DIR="${BACKUP_DIR}" deploy/backup.sh
api PUT /admin/backup '{"target":"s3"}' > /dev/null
expect_code 2 "нет секрета S3" env BACKUP_S3_SECRET_ACCESS_KEY= deploy/backup.sh
grep -q "BACKUP_S3_SECRET_ACCESS_KEY обязателен" "${STATE}/last.out" || fail "нет сообщения о секрете S3"
expect_code 2 "ключ шифрования обязателен для выгрузки" env BACKUP_ENCRYPTION_RECIPIENT= deploy/backup.sh
[[ -z "$(local_names)" ]] || fail "при некорректных настройках бэкап не должен создаваться"
r="$(curl -s -b "${JAR}" -X PUT -H "Origin: ${BASE_URL}" -H "X-CSRF-Token: $(csrf)" -H "Content-Type: application/json" \
  -d '{"keep_local":0}' "${BASE_URL}/api/admin/backup")"
[[ "$(json "d['error']['code']" <<< "${r}")" == "invalid_backup_setting" ]] || fail "UI принял keep_local=0"
ok "некорректные параметры отклоняются с понятной ошибкой, бэкап не начинается"

# ---------------------------------------------------------------- 2. шифрование
api PUT /admin/backup '{"target":"none","keep_local":2}' > /dev/null
expect_code 0 "бэкап (цель none)" backup
name="$(local_names | tail -1)"
[[ "${name}" == *.tar.age ]] || fail "локальная копия не зашифрована: ${name}"
[[ "$(head -c 21 "${BACKUP_DIR}/${name}")" == "age-encryption.org/v1" ]] || fail "нет заголовка age"
mkdir -p "${STATE}/decrypt" && rm -rf "${STATE}/decrypt"/*
MSYS_NO_PATHCONV=1 docker run --rm -i --user "$(id -u):$(id -g)" -v "${IDENTITY}:/key:ro" "${TOOLS}" age -d -i /key \
  < "${BACKUP_DIR}/${name}" | tar -C "${STATE}/decrypt" -xf -
inner="$(ls -1 "${STATE}/decrypt")"
( cd "${STATE}/decrypt/${inner}" && sha256sum -c --quiet SHA256SUMS ) || fail "контрольные суммы после расшифровки"
grep -q '^alembic_revision=0007_backup_runs$' "${STATE}/decrypt/${inner}/MANIFEST" || fail "MANIFEST"
if MSYS_NO_PATHCONV=1 docker run --rm -i "${TOOLS}" sh -c 'age-keygen -o /tmp/k 2>/dev/null && age -d -i /tmp/k' \
    < "${BACKUP_DIR}/${name}" > /dev/null 2>&1; then
  fail "архив расшифровался чужим ключом"
fi
rm -rf "${STATE}/decrypt"
ok "копия зашифрована age, расшифровывается только ключом администратора, контрольные суммы совпадают"

# ---------------------------------------------------------------- 3. каталог-цель, ротация
api PUT /admin/backup '{"target":"local","remote_prune":"script","keep_remote_daily":1,"keep_remote_weekly":0,"keep_remote_monthly":0}' > /dev/null
# «Старые» копии во внешнем каталоге: по схеме 1 ежедневная они должны быть удалены скриптом.
for old in 20260901T030000Z 20260902T030000Z; do
  cp "${BACKUP_DIR}/${name}" "${OFFSITE}/${old}.tar.age"
  ( cd "${OFFSITE}" && sha256sum "${old}.tar.age" > "${old}.tar.age.sha256" )
done
sleep 1
expect_code 0 "бэкап в каталог" backup
newest="$(local_names | tail -1)"
[[ -f "${OFFSITE}/${newest}" ]] || fail "копия не выгружена в каталог"
[[ "$(sha256sum < "${OFFSITE}/${newest}" | cut -d' ' -f1)" == "$(cut -d' ' -f1 < "${OFFSITE}/${newest}.sha256")" ]] || fail "sha256"
[[ "$(offsite_names)" == "${newest}" ]] || fail "ротация во внешнем каталоге: $(offsite_names | tr '\n' ' ')"
sleep 1
expect_code 0 "второй бэкап в каталог" backup
[[ "$(count "$(local_names)")" == 2 ]] || fail "локально должно остаться 2 копии (BACKUP_KEEP_LOCAL из UI): $(local_names | tr '\n' ' ')"
[[ "$(offsite_names)" == "$(local_names | tail -1)" ]] || fail "во внешнем каталоге должен остаться только новейший"
run="$(api GET /admin/backup)"
[[ "$(json "d['runs'][0]['status'], d['runs'][0]['uploaded'], d['runs'][0]['verified'], d['runs'][0]['pruned_remote'], d['runs'][0]['trigger']" <<< "${run}")" \
  == "('success', True, True, 1, 'manual')" ]] || fail "отчёт о запуске в UI: $(json "d['runs'][0]" <<< "${run}")"
ok "выгрузка в каталог с проверкой SHA-256; ротация локальных (2) и внешних копий (схема 1/0/0); отчёт виден в UI"

# ---------------------------------------------------------------- 4. сбой выгрузки
api PUT /admin/backup '{"target":"s3","remote_prune":"lifecycle"}' > /dev/null
before="$(local_names)"
sleep 1
expect_code 1 "выгрузка с неверным секретом S3" env BACKUP_S3_SECRET_ACCESS_KEY=wrong-secret deploy/backup.sh
after="$(local_names)"
[[ "$(count "${after}")" == 3 ]] || fail "при неудачной выгрузке локальные копии не удаляются: ${after}"
grep -qF "${before%%$'\n'*}" <<< "${after}" || fail "удалена старая копия"
sleep 2
docker logs cai-e2e-hook 2>&1 | grep -q '"error_code":"upload_failed"' || fail "нет уведомления webhook"
docker logs cai-e2e-hook 2>&1 | grep -q 'wrong-secret' && fail "секрет попал в уведомление"
run="$(api GET /admin/backup)"
[[ "$(json "d['runs'][0]['status'], d['runs'][0]['error_code']" <<< "${run}")" == "('failed', 'upload_failed')" ]] || fail "отчёт об ошибке"
ok "сбой выгрузки: код 1, локальные копии сохранены, уведомление webhook (без секретов), ошибка в UI"

# ---------------------------------------------------------------- 5. S3
sleep 1
expect_code 0 "бэкап в S3" backup
s3_newest="$(s3_names | tail -1)"
[[ "${s3_newest}" == "$(local_names | tail -1)" ]] || fail "копия не выгружена в S3"
[[ "$(count "$(local_names)")" == 2 ]] || fail "после успеха — ротация до 2"
grep -qE "проверка: MD5 объекта в хранилище|сверка скачанной копии" "${BACKUP_DIR}/backup.log" || fail "нет записи о проверке копии в S3"
sleep 1
expect_code 0 "второй бэкап в S3" backup
[[ "$(count "$(s3_names)")" == 2 ]] || fail "lifecycle: скрипт не удаляет копии в S3"
backup --list > "${STATE}/last.out"
grep -qF "$(s3_names | tail -1)" "${STATE}/last.out" || fail "--list не показывает копии S3"
ok "выгрузка в S3 (rclone) с проверкой копии в хранилище ($(grep -oE "проверка: MD5 объекта в хранилище|сверка скачанной копии" "${BACKUP_DIR}/backup.log" | tail -1)); режим lifecycle не удаляет внешние копии; --list"

# ---------------------------------------------------------------- 6. параллельный запуск
if command -v flock > /dev/null 2>&1; then
  flock "${BACKUP_DIR}/.lock" sleep 20 &
else
  sleep 20 &
  mkdir "${BACKUP_DIR}/.lock.d" && echo "$!" > "${BACKUP_DIR}/.lock.d/pid"
fi
holder=$!
sleep 1
expect_code 3 "ручной запуск при занятой блокировке" backup
expect_code 0 "опрос планировщика при занятой блокировке" backup --scheduled
kill "${holder}" 2> /dev/null || true
wait "${holder}" 2> /dev/null || true
rm -rf "${BACKUP_DIR}/.lock.d"
ok "параллельный запуск блокируется (ручной — код 3, планировщик — пропуск)"

# ---------------------------------------------------------------- 7. расписание и «Запустить сейчас»
runs_before="$(json "len(d['runs'])" <<< "$(api GET /admin/backup)")"
expect_code 0 "опрос: последний запуск позже срабатывания расписания" backup --scheduled
[[ "$(json "len(d['runs'])" <<< "$(api GET /admin/backup)")" == "${runs_before}" ]] || fail "лишний запуск по расписанию"
api POST /admin/backup/run > /dev/null
expect_code 0 "опрос: запрос есть, но не прошёл BACKUP_MIN_INTERVAL_MINUTES" backup --scheduled
grep -q "BACKUP_MIN_INTERVAL_MINUTES" "${BACKUP_DIR}/backup.log" || fail "нет записи о минимальном интервале"
[[ "$(json "len(d['runs'])" <<< "$(api GET /admin/backup)")" == "${runs_before}" ]] || fail "запуск раньше минимального интервала"
echo 0 > "${BACKUP_DIR}/.last-run"
expect_code 0 "опрос: выполнение запроса из UI" backup --scheduled
state="$(api GET /admin/backup)"
[[ "$(json "d['runs'][0]['trigger'], d['runs'][0]['status'], d['requested_at'], d['scheduler_seen_at'] is not None" <<< "${state}")" \
  == "('requested', 'success', None, True)" ]] || fail "запрос из UI не выполнен: $(json "d['runs'][0]" <<< "${state}")"
next_minute() { sleep $((62 - 10#$(date +%S))); }
# Расписание «каждую минуту» (из UI): после смены минуты планировщик запускает бэкап по расписанию.
api PUT /admin/backup '{"schedule":"* * * * *"}' > /dev/null
next_minute
echo 0 > "${BACKUP_DIR}/.last-run"
expect_code 0 "опрос: срабатывание расписания" backup --scheduled
[[ "$(json "d['runs'][0]['trigger'], d['runs'][0]['status']" <<< "$(api GET /admin/backup)")" == "('scheduled', 'success')" ]] \
  || fail "бэкап по расписанию не выполнен"
# Расписание выключено в UI — по расписанию не запускается.
api PUT /admin/backup '{"schedule_enabled":false}' > /dev/null
runs_before="$(json "len(d['runs'])" <<< "$(api GET /admin/backup)")"
next_minute
echo 0 > "${BACKUP_DIR}/.last-run"
expect_code 0 "опрос при выключенном расписании" backup --scheduled
[[ "$(json "len(d['runs'])" <<< "$(api GET /admin/backup)")" == "${runs_before}" ]] || fail "запуск при выключенном расписании"
api PUT /admin/backup '{"reset":["schedule","schedule_enabled"]}' > /dev/null
ok "планировщик: без повода не запускает, соблюдает минимальный интервал, выполняет «Запустить сейчас» и расписание из UI, выключенное расписание не срабатывает"

# ---------------------------------------------------------------- 8. полный цикл: удалить локальные → скачать → восстановить
full="$(s3_names | tail -1)"
rm -rf "${BACKUP_DIR:?}"/2*
[[ -z "$(local_names)" ]] || fail "локальные копии не удалены"
expect_code 0 "скачивание из S3" backup --fetch "${full}"
[[ -f "${BACKUP_DIR}/restore/${full}" ]] || fail "копия не скачана"
expect_code 0 "восстановление из зашифрованной копии" deploy/restore.sh "${BACKUP_DIR}/restore/${full}" --identity "${IDENTITY}" --confirm
grep -q "\[restore\] расшифровка" "${STATE}/last.out" || fail "не было расшифровки"
for _ in $(seq 1 60); do curl -fsS "${BASE_URL}/api/health/ready" > /dev/null 2>&1 && break; sleep 2; done
api PUT /admin/backup '{"schedule":"0 3 * * *"}' > /dev/null 2>&1 || true
ok "бэкап → выгрузка в S3 → удаление локальных копий → скачивание → восстановление (проверка данных — Playwright)"

# ---------------------------------------------------------------- 9. расписание на хосте: cron и systemd (в контейнерах Linux)
MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT}:/opt/cai:ro" alpine:3.22 sh -c '
  apk add -q bash > /dev/null
  bash /opt/cai/deploy/backup-schedule.sh install --cron --repo-dir /opt/cai > /dev/null
  bash /opt/cai/deploy/backup-schedule.sh install --cron --repo-dir /opt/cai > /dev/null
  [ "$(crontab -l | grep -c contract-ai-analyzer-backup)" = 1 ] || exit 11
  crontab -l | grep -q "^\*/5 \* \* \* \* cd /opt/cai && ./deploy/backup.sh --scheduled" || exit 12
  bash /opt/cai/deploy/backup-schedule.sh status --cron --repo-dir /opt/cai | grep -q "Установлен" || exit 13
  bash /opt/cai/deploy/backup-schedule.sh uninstall --cron --repo-dir /opt/cai > /dev/null
  ! crontab -l 2> /dev/null | grep -q contract-ai-analyzer-backup || exit 14
' || fail "cron: install/update/status/uninstall"
MSYS_NO_PATHCONV=1 docker run --rm -v "${ROOT}:/opt/cai:ro" debian:trixie-slim bash -c '
  apt-get update -qq > /dev/null && apt-get install -y -qq systemd > /dev/null 2>&1
  mkdir -p /tmp/units
  bash /opt/cai/deploy/backup-schedule.sh install --systemd --no-systemctl --unit-dir /tmp/units --repo-dir /opt/cai --user root > /dev/null
  grep -q "^OnCalendar=\*:0/5$" /tmp/units/contract-ai-analyzer-backup.timer || exit 21
  grep -q "^ExecStart=/opt/cai/deploy/backup.sh --scheduled$" /tmp/units/contract-ai-analyzer-backup.service || exit 22
  SYSTEMD_LOG_LEVEL=err systemd-analyze verify /tmp/units/contract-ai-analyzer-backup.timer /tmp/units/contract-ai-analyzer-backup.service 2>&1 \
    | grep -v "docker.service" | grep -q . && exit 23
  bash /opt/cai/deploy/backup-schedule.sh uninstall --systemd --no-systemctl --unit-dir /tmp/units > /dev/null
  [ ! -e /tmp/units/contract-ai-analyzer-backup.timer ] || exit 24
' || fail "systemd: unit-файлы"
ok "deploy/backup-schedule.sh: crontab (установка, повтор без дублей, статус, удаление) и systemd timer (systemd-analyze verify)"

# ---------------------------------------------------------------- 10. секреты не попали в журналы
for leak in "${S3_SECRET}" "AGE-SECRET-KEY"; do
  if grep -qF -- "${leak}" "${BACKUP_DIR}/backup.log" || docker compose logs --no-color 2>&1 | grep -qF -- "${leak}"; then
    fail "секрет в журнале"
  fi
done
ok "секрет S3 и приватный ключ age не попали в журнал бэкапа и логи сервисов"
echo "[backup-test] все проверки этапа 08A пройдены (${STEP})"
