# Contract AI Analyzer

Русскоязычное веб-приложение для предварительного анализа договоров (DOC, DOCX, PDF с текстовым слоем): извлечение текста, поиск юридических и коммерческих рисков с помощью LLM (OpenRouter или локальный Ollama), серверная проверка цитат, отчёты.

> Результаты — поддержка анализа, а не юридическая консультация.

**Статус:** в разработке, см. [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md). Сейчас реализован каркас (этап 02): API с health/readiness, схема БД и миграции, очередь задач и worker, frontend-оболочка, Docker Compose. Загрузка документов, авторизация и анализ — следующие этапы.

## Быстрый старт

```bash
cp .env.example .env       # заполните секреты (команды генерации внутри файла)
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/health/ready
```

Открыть: http://localhost:8080

Подробно — [docs/OPERATIONS.md](docs/OPERATIONS.md): разработка без Docker, тесты, миграции, резервное копирование и восстановление, production.

## Структура

| Каталог | Содержимое |
|---|---|
| `backend/` | FastAPI, SQLAlchemy 2, Alembic, worker |
| `frontend/` | React + TypeScript + Vite + Tailwind, nginx-образ |
| `converter/` | изолированный LibreOffice для DOC |
| `deploy/` | скрипты бэкапа и восстановления |
| `docs/` | архитектура, модель угроз, план, статус, эксплуатация |
| `CLAUDE.md` | инструкции и контекст для продолжения разработки в Claude Code |
