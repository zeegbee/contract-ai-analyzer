"""Клиент изолированного конвертера LibreOffice (ARCHITECTURE §8.3).

DOC → DOCX (дальше — обычный путь DOCX) и DOCX → PDF (для разметки страниц).
Ошибки:
- ConverterUnavailable — сервис недоступен/перегружен; задачу имеет смысл повторить позже;
- ExtractionFailed — файл не конвертируется (повтор не поможет).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.ingestion.model import ExtractionFailed

MAX_RESPONSE_BYTES = 200 * 1024 * 1024
_ALLOWED = {("doc", "docx"), ("docx", "pdf")}


class ConverterUnavailable(Exception):
    def __init__(self, code: str = "converter_unavailable") -> None:
        super().__init__(code)
        self.code = code


class ConverterClient:
    def __init__(
        self, base_url: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.transport = transport

    async def convert(self, source: Path, source_format: str, target_format: str, destination: Path) -> int:
        """Конвертирует файл и пишет результат в `destination`. Возвращает размер результата."""
        if (source_format, target_format) not in _ALLOWED:
            raise ValueError("unsupported conversion")
        timeout = httpx.Timeout(self.timeout, connect=5.0)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout, follow_redirects=False, transport=self.transport, trust_env=False
                ) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/convert",
                    params={"from": source_format, "to": target_format},
                    content=await asyncio.to_thread(source.read_bytes),
                    headers={"Content-Type": "application/octet-stream"},
                ) as response,
            ):
                if response.status_code == 200:
                    return await _save(response, destination)
                await response.aread()
                code = _error_code(response)
        except httpx.TimeoutException as exc:
            raise ExtractionFailed("conversion_timeout") from exc
        except httpx.TransportError as exc:
            raise ConverterUnavailable() from exc
        if response.status_code in (429, 502, 503):
            raise ConverterUnavailable(
                "converter_busy" if response.status_code == 429 else "converter_unavailable"
            )
        if response.status_code == 504 or code == "timeout":
            raise ExtractionFailed("conversion_timeout")
        raise ExtractionFailed("conversion_failed")


async def _save(response: httpx.Response, destination: Path) -> int:
    size = 0
    out = await asyncio.to_thread(destination.open, "wb")
    try:
        with out:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ExtractionFailed("conversion_failed")
                out.write(chunk)
        if size == 0:
            raise ExtractionFailed("conversion_failed")
    except BaseException:
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise
    return size


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""
