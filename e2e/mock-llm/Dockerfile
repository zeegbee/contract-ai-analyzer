# Mock-сервер с API Ollama — только для e2e-стенда (e2e/compose.e2e.yaml).
FROM python:3.13-slim-trixie
RUN useradd --system --uid 10001 mock
COPY server.py /app/server.py
USER 10001
EXPOSE 11434
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=2)"]
CMD ["python", "-u", "/app/server.py"]
