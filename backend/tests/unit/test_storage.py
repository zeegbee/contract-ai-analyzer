from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.storage import LocalStorage, StorageError


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    s = LocalStorage(tmp_path / "storage")
    s.ensure_layout()
    return s


def test_write_read_delete(storage: LocalStorage) -> None:
    key = storage.new_key("originals")
    assert storage.write_bytes(key, b"hello") == 5
    with storage.open(key) as f:
        assert f.read() == b"hello"
    assert storage.exists(key)
    storage.delete(key)
    assert not storage.exists(key)
    storage.delete(key)  # повторное удаление не ошибка


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "originals/../../x",
        "originals/abc",
        "public/0123456789abcdef0123456789abcdef",
        "originals/0123456789abcdef0123456789abcdef/../x",
        "/abs/0123456789abcdef0123456789abcdef",
        "originals\\0123456789abcdef0123456789abcdef",
    ],
)
def test_rejects_unsafe_keys(storage: LocalStorage, key: str) -> None:
    with pytest.raises(StorageError):
        storage.open(key)


def test_size_limit_leaves_no_partial_file(storage: LocalStorage) -> None:
    key = storage.new_key("originals")
    with pytest.raises(StorageError, match="file_too_large"):
        storage.write_stream(key, io.BytesIO(b"x" * 2048), max_bytes=1024)
    assert not storage.exists(key)
    assert list((storage.root / "originals").iterdir()) == []


def test_keys_are_random_and_namespaced(storage: LocalStorage) -> None:
    a, b = storage.new_key("exports", ".pdf"), storage.new_key("exports", ".pdf")
    assert a != b
    assert a.startswith("exports/") and a.endswith(".pdf")


def test_check_writable(storage: LocalStorage) -> None:
    assert storage.check_writable()
