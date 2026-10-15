# syntax=docker/dockerfile:1.7
# Образ backend: API (`python -m app.server`), worker (`python -m app.worker`) и миграции (`alembic upgrade head`).

FROM python:3.13-slim-trixie AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml requirements.lock ./
COPY app ./app
RUN pip install --require-hashes --no-deps -r requirements.lock \
 && pip install --no-deps .

FROM python:3.13-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    ENV=production STORAGE_DIR=/data/storage
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data/storage /app \
 && chown -R app:app /data/storage \
 && chmod 0700 /data/storage
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root app ./app
USER app:app
VOLUME ["/data/storage"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3).status == 200 else 1)"]
CMD ["python", "-m", "app.server"]
