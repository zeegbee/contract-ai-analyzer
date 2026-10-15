"""Файловое хранилище вне публичного каталога.

Ключи генерирует сервер (`{namespace}/{uuid}`), пользовательские имена файлов в пути не участвуют.
Запись атомарная: временный файл в том же каталоге → fsync → rename.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

_KEY_RE = re.compile(r"^(originals|converted|text|exports|tmp)/[0-9a-f]{32}(\.[a-z0-9]{1,8})?$")
_CHUNK = 1024 * 1024


class StorageError(Exception):
    pass


class StorageBackend(Protocol):
    def new_key(self, namespace: str, suffix: str = "") -> str: ...
    def write_stream(self, key: str, stream: BinaryIO, *, max_bytes: int | None = None) -> int: ...
    def write_bytes(self, key: str, data: bytes) -> int: ...
    def open(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def check_writable(self) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def ensure_layout(self) -> None:
        for ns in ("originals", "converted", "text", "exports", "tmp"):
            (self.root / ns).mkdir(mode=0o700, parents=True, exist_ok=True)

    def new_key(self, namespace: str, suffix: str = "") -> str:
        key = f"{namespace}/{uuid.uuid4().hex}{suffix}"
        self._validate_key(key)
        return key

    def _validate_key(self, key: str) -> None:
        if not _KEY_RE.match(key):
            raise StorageError("invalid_storage_key")

    def _path(self, key: str) -> Path:
        self._validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("invalid_storage_key")
        return path

    def write_stream(self, key: str, stream: BinaryIO, *, max_bytes: int | None = None) -> int:
        target = self._path(key)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        written = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise StorageError("file_too_large")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
        return written

    def write_bytes(self, key: str, data: bytes) -> int:
        return self.write_stream(key, io.BytesIO(data))

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(key).unlink()

    def iter_keys(self, namespace: str) -> Iterator[str]:
        base = self.root / namespace
        if not base.is_dir():
            return
        for entry in base.iterdir():
            key = f"{namespace}/{entry.name}"
            if entry.is_file() and _KEY_RE.match(key):
                yield key

    def check_writable(self) -> bool:
        probe = self.root / "tmp" / f".probe-{uuid.uuid4().hex}"
        try:
            probe.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError:
            return False
        return True
