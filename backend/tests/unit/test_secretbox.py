from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.security.secretbox import SecretBox, SecretsNotConfigured, SecretUndecryptable, key_fingerprint

KEY = "sk-or-v1-abcdefabcdefabcdefabcdef"


def _key() -> str:
    return Fernet.generate_key().decode()


def test_encrypt_roundtrip_and_ciphertext_hides_secret() -> None:
    k = _key()
    box = SecretBox([SecretStr(k)])
    ciphertext, key_id = box.encrypt(KEY)
    assert KEY.encode() not in ciphertext
    assert key_id == key_fingerprint(k) and k not in key_id
    assert box.decrypt(ciphertext) == KEY
    assert k not in repr(box)


def test_rotation_old_key_decrypts_and_rotate_uses_primary() -> None:
    old, new = _key(), _key()
    ciphertext, old_id = SecretBox([SecretStr(old)]).encrypt(KEY)
    rotated_box = SecretBox([SecretStr(new), SecretStr(old)])
    assert rotated_box.decrypt(ciphertext) == KEY
    rotated, new_id = rotated_box.rotate(ciphertext)
    assert new_id != old_id
    assert SecretBox([SecretStr(new)]).decrypt(rotated) == KEY


def test_wrong_key_and_missing_configuration() -> None:
    ciphertext, _ = SecretBox([SecretStr(_key())]).encrypt(KEY)
    with pytest.raises(SecretUndecryptable):
        SecretBox([SecretStr(_key())]).decrypt(ciphertext)
    with pytest.raises(SecretsNotConfigured):
        SecretBox([]).encrypt(KEY)
