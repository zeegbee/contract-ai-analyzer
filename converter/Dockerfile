# syntax=docker/dockerfile:1.7
# Изолированный конвертер: LibreOffice headless без Java, без root, без сети наружу (см. compose.yaml).
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 HOME=/tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui python3 fonts-dejavu-core fonts-liberation \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10002 conv \
 && useradd --system --uid 10002 --gid conv --home-dir /tmp --shell /usr/sbin/nologin conv
WORKDIR /srv
COPY server.py ./
USER conv:conv
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3).status == 200 else 1)"]
CMD ["python3", "/srv/server.py"]
