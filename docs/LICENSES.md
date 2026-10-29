# Лицензии сторонних компонентов

Документ фиксирует лицензии ключевых зависимостей и ограничения, которые из них следуют. Это справка для владельца продукта, а не юридическое заключение: перед изменением модели распространения проконсультируйтесь с юристом.

## PyMuPDF — AGPL-3.0

**Где используется:** извлечение текста из PDF (`backend/app/ingestion/pdf.py`): разбор загруженных PDF и PDF-рендера DOCX для определения страниц. Устанавливается в образ backend/worker из `backend/requirements.lock` (`pymupdf==1.28.2`).

**Лицензия:** двойная — GNU Affero GPL 3.0 или коммерческая лицензия Artifex (по метаданным пакета: «Dual Licensed — GNU AFFERO GPL 3.0 or Artifex Commercial License»).

**Решение (2026-10-16):** приложение планируется **только для внутреннего использования** в организации, PyMuPDF используется под AGPL-3.0.

**Решение (2026-10-29):** исходный код публикуется в открытом репозитории GitHub под **AGPL-3.0-only** (файл `LICENSE`, вариант 2 ниже). Это совместимо с PyMuPDF под AGPL-3.0; любой, кто предоставляет доступ к изменённой версии по сети, обязан открыть её исходный код на тех же условиях. Готовые образы Docker не публикуются; при их публикации — указать авторство и лицензию CC BY-SA 3.0 словарей OpenCorpora (pymorphy3-dicts-ru, см. таблицу).

**Что важно знать:**
- AGPL распространяет требования GPL на работу по сети: если пользователи, которые **не являются сотрудниками** организации-владельца (клиенты, контрагенты, внешние подрядчики, пользователи SaaS), получают доступ к приложению через сеть, им нужно предоставить исходный код всего приложения, использующего PyMuPDF, на условиях AGPL-3.0.
- Внутреннее использование сотрудниками одной организации обычно не считается предоставлением программы третьим лицам. Пограничные случаи (доступ сотрудников дочерних компаний, аутсорсеров, внешних юристов) нужно оценивать отдельно.
- Передача образа или исходного кода третьим лицам (установка у клиента, продажа, публикация образа) — это распространение: тоже требуется AGPL-совместимое лицензирование всего приложения или коммерческая лицензия Artifex.

**Что сделать, если модель использования изменится** (внешние пользователи, SaaS, поставка клиентам):
1. купить коммерческую лицензию Artifex, **или**
2. открыть исходный код приложения под AGPL-3.0, **или**
3. заменить PyMuPDF на библиотеку с разрешительной лицензией, например **pypdfium2** (Apache-2.0 / BSD-3-Clause). Замена затрагивает только `backend/app/ingestion/pdf.py` (и генерацию тестовых PDF в `backend/tests/docfactory.py`); интерфейс модуля — `extract_pdf(path, limits) -> RawExtraction` и `pdf_page_texts(path, max_pages)` — остаётся прежним. После замены нужно перепроверить порядок блоков, колонтитулы и определение страниц без текста (тесты `tests/unit/test_ingestion_*.py`).

Пересматривайте этот раздел при любом изменении круга пользователей или способа поставки.

## Остальные ключевые компоненты

Версии — по `backend/requirements.lock`, `frontend/package-lock.json` и образу `converter` на 2026-10-16.

| Компонент | Версия | Лицензия | Примечание |
|---|---|---|---|
| PyMuPDF | 1.28.2 | AGPL-3.0 или коммерческая | см. выше |
| psycopg / psycopg-binary | 3.3.6 | LGPL-3.0-only | используется как библиотека без изменений — ограничений для приложения не создаёт |
| LibreOffice (контейнер `converter`, пакет Debian `libreoffice-writer-nogui`) | 25.2.3 | MPL-2.0 | запускается отдельной программой в изолированном контейнере |
| lxml | 6.1.3 | BSD-3-Clause | |
| olefile | 0.47 | BSD | |
| httpx | 0.28.1 | BSD-3-Clause | |
| uvicorn | 0.54.0 | BSD-3-Clause | |
| cryptography | 50.0.2 | Apache-2.0 или BSD-3-Clause | |
| structlog | 26.1.0 | MIT или Apache-2.0 | |
| FastAPI, SQLAlchemy, Alembic, Pydantic, argon2-cffi | — | MIT | |
| python-docx | 1.2.0 | MIT | экспорт DOCX (с этапа 07), генерация тестовых фикстур |
| reportlab | 5.0.1 | BSD | экспорт PDF |
| openpyxl / et-xmlfile | 3.1.5 / 2.0.0 | MIT | экспорт XLSX |
| Pillow (зависимость reportlab и python-docx) | 13.0.0 | MIT-CMU (HPND) | |
| Шрифты DejaVu Sans (`backend/app/exports/fonts/`, взяты из пакета matplotlib 3.11.2) | 2.35 | Bitstream Vera License + public domain (`LICENSE_DEJAVU`) | встраиваются в PDF-отчёт; лицензия разрешает распространение и встраивание |
| natasha (модели NER `slovnet_ner_news_v1`, эмбеддинги `navec_news_v1`), slovnet, navec, razdel, yargy, ipymarkup | 1.6.0 / 0.6.0 / 0.10.0 / 0.5.0 / 0.16.0 / 0.9.0 | MIT | маскировка (этап 10): из `natasha` используются только файлы моделей (пакет не импортируется) |
| pymorphy3 + pymorphy3-dicts-ru | 2.0.6 / 2.4.417150 | код — MIT; **данные словаря — CC BY-SA 3.0** | морфология ФИО и словаря. Проверено 2026-10-29 по метаданным установленного пакета `pymorphy3-dicts-ru`: «License for Python code in this package is MIT. The data is licensed under Creative Commons Attribution-Share Alike» (creativecommons.org/licenses/by-sa/3.0/); источник данных — OpenCorpora (`meta.json`: `source` opencorpora.org, ревизия 417150). Сайт OpenCorpora при проверке был недоступен (HTTP 522). В репозитории словари не хранятся (устанавливаются pip в образ) — публикация исходного кода их не распространяет. При распространении образа: указать авторство OpenCorpora и лицензию CC BY-SA 3.0, изменённые словари — на тех же условиях; на лицензию кода приложения это не влияет |
| pymorphy2 + pymorphy2-dicts-ru, docopt, DAWG-Python, DAWG2-Python | 0.9.1 / 2.4.417127 / 0.6.2 / 0.7.2 / 0.9.0 | MIT; данные pymorphy2-dicts-ru — CC BY-SA 3.0 (по метаданным пакета) | транзитивные зависимости natasha/pymorphy3; pymorphy2 не используется (словари — как у pymorphy3) |
| intervaltree (зависимость ipymarkup) | 3.2.1 | Apache-2.0 | транзитивная, не используется |
| numpy | 2.5.4 | BSD-3-Clause (+ включённые компоненты 0BSD, MIT, Zlib, CC0) | зависимость navec/slovnet |
| google-re2 | 1.1.20251105 | BSD-3-Clause | пользовательские шаблоны маскировки (линейное время) |
| phonenumberslite | 9.0.41 | Apache-2.0 | распознавание телефонов |
| React, React Router, TanStack Query, Zod, React Hook Form | — | MIT | |
| PostgreSQL (образ `postgres:17-alpine`) | 17 | PostgreSQL License | |
| nginx (образ `nginx-unprivileged`) | — | BSD-2-Clause | |
| certifi (зависимость httpx) | 2026.10.19 | MPL-2.0 | используется без изменений (набор корневых сертификатов); не ниже 2026.10.19 — PYSEC-2026-4320 |
| Playwright (`@playwright/test`, `playwright-core`) — только разработка | 1.63.0 | Apache-2.0 | e2e-тесты, в образы не входит |
| axe-core, `@axe-core/playwright` — только разработка | 4.13.0 | MPL-2.0 | аудит доступности в e2e, в образы не входит |

Полный перечень транзитивных зависимостей — в lock-файлах.

### Проверка лицензий (этап 08, 2026-10-17)

- Python (`pip-licenses` по окружению backend, включая dev-зависимости, 63 пакета): копилефт — только PyMuPDF (AGPL-3.0, см. выше), psycopg/psycopg-binary (LGPL-3.0), certifi и pathspec (MPL-2.0; pathspec — только dev, через mypy/ruff). Остальные — MIT, BSD, Apache-2.0, PSF, ISC, HPND.
- npm production-зависимости frontend (по `npm ls --omit=dev`): все установленные пакеты — MIT.
- Уязвимости: `pip-audit -r backend/requirements.lock` и по dev-окружению — «No known vulnerabilities found»; `npm audit` — 0 уязвимостей.

Команды — `docs/OPERATIONS.md` §3.
