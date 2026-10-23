"""Encryption for secrets kept in the database (provider API keys).

The key comes from SECRET_KEY when set, otherwise from a ``secret.key`` file
created next to the database on first use. Back it up with the database:
without it, stored provider keys cannot be read and must be entered again.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)
PREFIX = "enc:v1:"


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def for_database(cls, database_path: str, secret_key: str = "") -> "SecretBox":
        if secret_key:
            digest = hashlib.sha256(secret_key.encode()).digest()
            return cls(base64.urlsafe_b64encode(digest))
        if database_path in ("", ":memory:"):
            return cls(Fernet.generate_key())
        key_file = Path(database_path).parent / "secret.key"
        if not key_file.exists():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            # Create it readable by this user only.
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(Fernet.generate_key())
        return cls(key_file.read_bytes().strip())

    def seal(self, value: str) -> str:
        if not value or value.startswith(PREFIX):
            return value
        return PREFIX + self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str:
        """Decrypt a sealed value; plain values (saved before encryption) pass
        through, and a value that cannot be decrypted reads as empty."""
        if not value.startswith(PREFIX):
            return value
        try:
            return self._fernet.decrypt(value.removeprefix(PREFIX).encode()).decode()
        except InvalidToken:
            logger.warning(
                "A stored API key cannot be decrypted (was secret.key or "
                "SECRET_KEY changed?); enter it again on the Models page"
            )
            return ""

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """Raises ``InvalidToken`` when the key does not match."""
        return self._fernet.decrypt(token)
