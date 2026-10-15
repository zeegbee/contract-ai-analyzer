"""Изолированный сервис конвертации документов (LibreOffice headless).

Этап 02: каркас — только health. Конвертация DOC→DOCX и DOCX→PDF реализуется на этапе 04;
до этого `/convert` честно отвечает 501, а не имитирует результат.

Сервис запускается без root, без выхода в интернет (сеть `internal`), с read-only FS и tmpfs.
Используется только стандартная библиотека Python — меньше зависимостей в недоверенной зоне.
"""

from __future__ import annotations

import json
import os
import shutil
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


class Handler(BaseHTTPRequestHandler):
    server_version = "converter"
    sys_version = ""

    def _json(self, status: HTTPStatus, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - имя из BaseHTTPRequestHandler
        if self.path == "/health":
            ok = SOFFICE is not None
            self._json(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE, {"status": "ok" if ok else "no_soffice"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/convert"):
            self._json(HTTPStatus.NOT_IMPLEMENTED, {"error": "not_implemented"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Только метод/путь/статус, без содержимого.
        print(json.dumps({"event": "converter_request", "line": format % args}), flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 - внутренняя сеть


if __name__ == "__main__":
    main()
