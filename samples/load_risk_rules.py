"""Заводит на стенде тестовые правила рисков организации из samples/risk_rules.json.

Запуск из корня репозитория (нужен Администратор или Superadmin):
  CAI_PASSWORD='…' backend/.venv/Scripts/python samples/load_risk_rules.py [--url URL] [--email EMAIL]
(Linux/macOS: backend/.venv/bin/python). По умолчанию URL — http://127.0.0.1:8080 (должен быть
в ALLOWED_ORIGINS), e-mail — root@example.org. Пароль берётся из CAI_PASSWORD или спрашивается.

Правило с тем же названием (в том числе в архиве) повторно не создаётся. Коды RR-NNN присваивает
сервер по порядку создания: на чистом стенде правила получат RR-001…RR-003, как в samples/README.md.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import httpx

RULES = Path(__file__).resolve().parent / "risk_rules.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("CAI_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--email", default=os.environ.get("CAI_EMAIL", "root@example.org"))
    args = parser.parse_args()
    # Консоль Windows по умолчанию не в UTF-8 — без этого кириллица в выводе искажается.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    base = args.url.rstrip("/")
    password = os.environ.get("CAI_PASSWORD") or getpass.getpass(f"Пароль {args.email}: ")
    rules = json.loads(RULES.read_text(encoding="utf-8"))

    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=30) as client:
        res = client.post("/api/auth/login", json={"email": args.email, "password": password})
        if res.status_code != 200:
            print(f"Вход не выполнен: HTTP {res.status_code}", file=sys.stderr)
            return 1
        if res.json().get("must_change_password"):
            print("Сначала смените временный пароль в интерфейсе", file=sys.stderr)
            return 1
        client.headers["X-CSRF-Token"] = client.cookies.get("cai_csrf") or ""

        existing = {}
        for archived in (False, True):
            res = client.get("/api/admin/risk-rules", params={"archived": archived})
            if res.status_code != 200:
                print(
                    f"Список правил недоступен: HTTP {res.status_code} (нужна роль Администратор)",
                    file=sys.stderr,
                )
                return 1
            existing.update({r["title"]: r for r in res.json()["items"]})

        for rule in rules:
            found = existing.get(rule["title"])
            if found:
                note = " (в архиве)" if found.get("archived_at") else ""
                print(f"есть      {found['code']}  {rule['title']}{note}")
                continue
            res = client.post("/api/admin/risk-rules", json=rule)
            if res.status_code != 201:
                print(f"ошибка    HTTP {res.status_code}  {rule['title']}: {res.text[:200]}", file=sys.stderr)
                return 1
            print(f"создано   {res.json()['code']}  {rule['title']}")
        client.post("/api/auth/logout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
