#!/usr/bin/env bash
# Планировщик резервного копирования на хосте (Linux): systemd timer или строка crontab.
#
#   deploy/backup-schedule.sh install    установить/обновить (идемпотентно)
#   deploy/backup-schedule.sh uninstall  удалить
#   deploy/backup-schedule.sh status     показать состояние и последние строки журнала
#
# Таймер раз в 5 минут запускает `deploy/backup.sh --scheduled`; бэкап выполняется, только когда
# по расписанию (BACKUP_SCHEDULE или значение из UI) пора или Superadmin нажал «Запустить сейчас»,
# и не чаще BACKUP_MIN_INTERVAL_MINUTES. Поэтому после изменения расписания переустанавливать
# таймер не нужно. Docker socket контейнерам не выдаётся: команды docker выполняет хост.
#
# Параметры: --cron | --systemd (по умолчанию systemd, если он есть, иначе cron);
#            --unit-dir <каталог> (по умолчанию /etc/systemd/system), --no-systemctl — только записать
#            файлы unit (для проверки); --user <пользователь> — от чьего имени запускать (член группы docker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="contract-ai-analyzer-backup"
MARKER="# ${NAME}"
ACTION="${1:-}"
shift || true
MODE=""
UNIT_DIR="/etc/systemd/system"
SYSTEMCTL=1
RUN_USER="${SUDO_USER:-$(id -un)}"
while (($#)); do
  case "$1" in
    --cron) MODE="cron" ;;
    --systemd) MODE="systemd" ;;
    --unit-dir) UNIT_DIR="$2"; shift ;;
    --no-systemctl) SYSTEMCTL=0 ;;
    --user) RUN_USER="$2"; shift ;;
    --repo-dir) ROOT="$2"; shift ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "${MODE}" ]]; then
  if command -v systemctl > /dev/null 2>&1 && [[ -d /run/systemd/system ]]; then MODE="systemd"; else MODE="cron"; fi
fi
[[ "${ROOT}" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "Путь к репозиторию должен состоять из латиницы, цифр и . _ / -" >&2; exit 2; }
CRON_LINE="*/5 * * * * cd ${ROOT} && ./deploy/backup.sh --scheduled >> ${ROOT}/backups-cron.log 2>&1 ${MARKER}"

systemd_units() {
  cat > "${UNIT_DIR}/${NAME}.service" <<EOF
[Unit]
Description=Contract AI Analyzer: резервное копирование по расписанию (опрос)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/deploy/backup.sh --scheduled
# Бэкап может идти долго; повторный запуск таймера во время работы просто завершится (блокировка).
TimeoutStartSec=6h
Nice=10
IOSchedulingClass=idle
EOF
  cat > "${UNIT_DIR}/${NAME}.timer" <<EOF
[Unit]
Description=Contract AI Analyzer: опрос расписания резервного копирования каждые 5 минут

[Timer]
OnCalendar=*:0/5
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
}

case "${MODE}:${ACTION}" in
  systemd:install)
    systemd_units
    if ((SYSTEMCTL)); then
      systemctl daemon-reload
      systemctl enable --now "${NAME}.timer"
    fi
    echo "Установлено: ${UNIT_DIR}/${NAME}.timer (каждые 5 минут, пользователь ${RUN_USER})"
    ;;
  systemd:uninstall)
    if ((SYSTEMCTL)); then systemctl disable --now "${NAME}.timer" 2> /dev/null || true; fi
    rm -f "${UNIT_DIR}/${NAME}.service" "${UNIT_DIR}/${NAME}.timer"
    if ((SYSTEMCTL)); then systemctl daemon-reload; fi
    echo "Удалено: ${NAME}.timer"
    ;;
  systemd:status)
    if [[ -f "${UNIT_DIR}/${NAME}.timer" ]]; then echo "Установлен: ${UNIT_DIR}/${NAME}.timer"; else echo "Не установлен"; fi
    if ((SYSTEMCTL)); then
      systemctl list-timers "${NAME}.timer" --no-pager || true
      journalctl -u "${NAME}.service" -n 20 --no-pager 2> /dev/null || true
    fi
    ;;
  cron:install)
    { crontab -l 2> /dev/null | grep -vF "${MARKER}" || true; echo "${CRON_LINE}"; } | crontab -
    echo "Установлено в crontab пользователя $(id -un): ${CRON_LINE}"
    ;;
  cron:uninstall)
    { crontab -l 2> /dev/null | grep -vF "${MARKER}" || true; } | crontab -
    echo "Удалено из crontab"
    ;;
  cron:status)
    if crontab -l 2> /dev/null | grep -F "${MARKER}"; then echo "Установлен (crontab)"; else echo "Не установлен"; fi
    tail -n 20 "${ROOT}/backups-cron.log" 2> /dev/null || true
    ;;
  *)
    echo "Использование: $0 install|uninstall|status [--cron|--systemd]" >&2
    exit 2
    ;;
esac
echo "Журнал запусков: каталог BACKUP_DIR/backup.log; состояние — в UI «Настройки» → «Резервное копирование»."
