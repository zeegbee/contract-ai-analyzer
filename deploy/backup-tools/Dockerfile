# Инструменты резервного копирования для deploy/backup.sh и deploy/restore.sh: на хост ничего не ставится.
# age — шифрование, rclone — выгрузка в S3-совместимое хранилище, curl — уведомление webhook, tar.
FROM alpine:3.22
RUN apk add --no-cache age=~1.2 rclone=~1.69 curl tar \
 && adduser -D -u 10001 backup
USER 10001
ENTRYPOINT []
