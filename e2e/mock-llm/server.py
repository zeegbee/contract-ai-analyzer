"""Mock-сервер с API Ollama для e2e-стенда (только для тестов, в production не используется).

Приложение обращается к нему как к обычному провайдеру Ollama (`http://mock-llm:11434`, частная
сеть) — настоящий клиент, SSRF-guard, worker и серверная проверка цитат работают без изменений.
Ответы детерминированы и зависят только от запроса:

- структура договора — стороны вида «ООО «…»» из текста;
- анализ части текста — риск «одностороннее изменение» с дословной цитатой из фрагмента, где есть
  «в одностороннем порядке»; риск «неустойка» — из фрагмента со словом «неустойк»; и риск с
  ВЫДУМАННОЙ цитатой (её нет в тексте) — сервер обязан его отклонить; если в системном сообщении
  есть правило организации категории penalties (этап 11), риск «неустойка» ссылается на его код;
- межраздельный проход — одно отсутствующее условие (без цитаты);
- повторный запрос цитаты — модель отзывает риск;
- верификатор — «подтверждает» для всех цитат;
- проверка связи и прочие схемы — минимальный объект по JSON Schema.

Запускается без зависимостей: `python server.py` (порт 11434). Тексты запросов не логируются.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "e2e-mock:latest"
# «Медленная» модель: каждый ответ генерации задерживается — для проверки отмены и восстановления
# после аварийного перезапуска worker'а во время анализа.
SLOW_MODEL = "e2e-slow:latest"
SLOW_SECONDS = 4.0
MODELS = (MODEL, SLOW_MODEL)
FABRICATED_QUOTE = "Исполнитель вправе удерживать оплату без объяснения причин и уведомления"
_FRAGMENT = re.compile(r'<fragment id="(F\d{6})"[^>]*>(.*?)</fragment>', re.S)
_EVIDENCE = re.compile(r'<evidence no="(\d+)">')
_PARTY = re.compile(r"(?:ООО|АО|ПАО|ИП)\s+«[^»]{1,100}»")
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_PENALTY_RULE = re.compile(r'<rule code="([^"]+)" category="penalties"')


def minimal(schema: dict[str, Any]) -> Any:
    """Минимальное значение, удовлетворяющее упрощённой JSON Schema провайдера."""
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return minimal(options[0]) if options else None
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object":
        return {name: minimal(prop) for name, prop in schema.get("properties", {}).items()}
    return {"array": [], "string": "ok", "integer": 1, "number": 0.5, "boolean": True}.get(str(kind))


def fragments(text: str) -> list[tuple[str, str]]:
    return [(key, html.unescape(body)) for key, body in _FRAGMENT.findall(text)]


def sentence_with(text: str, needle: str) -> str | None:
    for sentence in _SENTENCE.split(text):
        if needle in sentence.lower() and len(sentence.split()) >= 4:
            return sentence.strip()
    return None


def risk(
    title: str, category: str, fragment_id: str | None, quote: str | None, **extra: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "category": category,
        "kind": "clause_risk",
        "severity": "high",
        "description": f"{title}: условие требует внимания",
        "reason": "Условие смещает баланс обязательств сторон",
        "consequences": "Дополнительные расходы и споры",
        "affected_party": "party_a",
        "recommendation": "Согласовать условие дополнительным соглашением",
        "suggested_revision": None,
        "model_confidence": 0.8,
        "evidence": [{"fragment_id": fragment_id, "quote": quote}] if fragment_id and quote else [],
        "search_scope": None,
        "rule_code": None,
    }
    item.update(extra)
    return item


def chunk_risks(system: str, user: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    frags = fragments(user)
    rule = _PENALTY_RULE.search(system)
    for key, text in frags:
        quote = sentence_with(text, "в одностороннем порядке")
        if quote:
            found.append(risk("Одностороннее изменение цены", "unilateral_changes", key, quote))
        quote = sentence_with(text, "неустойк")
        if quote:
            found.append(
                risk(
                    "Высокая неустойка",
                    "penalties",
                    key,
                    quote,
                    severity="medium",
                    rule_code=rule.group(1) if rule else None,
                )
            )
    if frags and "Часть 1 из" in user:
        # Цитаты нет в тексте договора: проверка доказательств должна отклонить этот риск.
        found.append(risk("Удержание оплаты", "payment", frags[0][0], FABRICATED_QUOTE))
    return {"risks": found, "notes": None}


def cross_section() -> dict[str, Any]:
    missing = risk(
        "Не определён порядок приёмки",
        "missing_clause",
        None,
        None,
        kind="missing_clause",
        severity="medium",
        search_scope="Просмотрены заголовки и ключевые условия договора",
    )
    return {"risks": [missing], "notes": None}


def answer(body: dict[str, Any]) -> dict[str, Any]:
    schema = body.get("format") if isinstance(body.get("format"), dict) else {}
    props = set(schema.get("properties", {}))
    messages = body.get("messages") or []
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    if props == {"risks", "notes"}:
        if system.startswith("Ты проверяешь договор целиком"):
            return cross_section()
        return chunk_risks(system, user)
    if "parties" in props:
        names = list(dict.fromkeys(_PARTY.findall(html.unescape(user))))[:2]
        labels = ["party_a", "party_b"]
        return {
            "contract_type": "договор поставки",
            "subject": "поставка товара",
            "parties": [{"label": labels[i], "name": n, "role": None} for i, n in enumerate(names)],
        }
    if "verdicts" in props:
        numbers = [int(n) for n in _EVIDENCE.findall(user)]
        return {
            "verdicts": [
                {"evidence_no": n, "verdict": "supports", "rationale": "Цитата прямо описывает условие"}
                for n in numbers
            ]
        }
    if "withdraw" in props:
        return {"withdraw": True, "evidence": [], "comment": "Подтверждающего текста нет"}
    return minimal(schema) if schema else {"status": "ok"}


class Handler(BaseHTTPRequestHandler):
    server_version = "e2e-mock-ollama"

    def log_message(self, fmt: str, *args: Any) -> None:  # без тела запроса
        sys.stderr.write(f"mock-llm {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        parsed = json.loads(self.rfile.read(length) or b"{}")
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:
        if self.path == "/api/version":
            self._send(200, {"version": "0.0.0-e2e"})
        elif self.path == "/api/tags":
            self._send(200, {"models": [{"name": m, "details": {"family": "mock"}} for m in MODELS]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/api/show":
            if body.get("model") not in MODELS:
                self._send(404, {"error": "model not found"})
                return
            self._send(200, {"model_info": {"mock.context_length": 32768}, "capabilities": ["completion"]})
        elif self.path == "/api/chat":
            if body.get("model") not in MODELS:
                self._send(404, {"error": "model not found"})
                return
            if body["model"] == SLOW_MODEL:
                time.sleep(SLOW_SECONDS)
            content = json.dumps(answer(body), ensure_ascii=False)
            self._send(
                200,
                {
                    "model": body["model"],
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 500,
                    "eval_count": 120,
                },
            )
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()  # noqa: S104 - внутри сети стенда
