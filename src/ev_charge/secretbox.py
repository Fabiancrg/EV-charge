"""Encryption of connector credentials (e.g. the Ohme account password) at rest.

A Fernet key is created on first use in {data_dir}/secret.key (readable by the owner only).
Losing that file means re-entering the charger credentials; the rest of the data is unaffected.
The key can also be given in the EV_CHARGE_SECRET_KEY environment variable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import CONFIG

_fernet: Fernet | None = None


class SecretError(Exception):
    """Stored credentials can't be decrypted (the key changed)."""


def _key_file() -> Path:
    return Path(CONFIG.data_dir or ".") / "secret.key"


def _box() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("EV_CHARGE_SECRET_KEY", "").encode()
        if not key:
            path = _key_file()
            if path.exists():
                key = path.read_bytes().strip()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                key = Fernet.generate_key()
                path.write_bytes(key)
                try:
                    path.chmod(0o600)
                except OSError:  # pragma: no cover - Windows
                    pass
        _fernet = Fernet(key)
    return _fernet


def encrypt_json(value: dict[str, Any]) -> str:
    return _box().encrypt(json.dumps(value).encode()).decode()


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return json.loads(_box().decrypt(token.encode()))
    except InvalidToken as exc:
        raise SecretError("The stored charger credentials can't be decrypted (secret.key changed): enter them again") from exc
