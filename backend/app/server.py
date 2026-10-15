"""Точка входа HTTP-сервера: `python -m app.server`.

Логирование настраивает приложение (`log_config=None`), стандартный access-лог uvicorn отключён.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from app.config import ConfigurationError, get_settings
from app.main import create_app


def main() -> None:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)  # логирование ещё не настроено
        sys.exit(2)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - внутри контейнера, наружу не публикуется
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxy_ips),
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
