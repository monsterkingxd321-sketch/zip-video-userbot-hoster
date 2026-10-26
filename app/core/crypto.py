"""Session encryption at rest.

Telethon session strings are encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
before they are written to the persistent volume or to MongoDB. Plaintext
session data only ever exists in process memory.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class SessionDecryptError(RuntimeError):
    """Raised when stored session data cannot be decrypted."""


class SessionCipher:
    """Encrypts/decrypts session strings using a Fernet key from configuration."""

    def __init__(self, key: str):
        key = (key or "").strip()
        if not key:
            raise ValueError(
                "SESSION_ENCRYPTION_KEY is empty. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:  # pragma: no cover - config error path
            raise ValueError(
                "SESSION_ENCRYPTION_KEY is not a valid Fernet key (expected 32 url-safe "
                "base64-encoded bytes). Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be a string")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes, *, user_id: Optional[int] = None) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            who = f" for user {user_id}" if user_id is not None else ""
            raise SessionDecryptError(
                f"Stored session data{who} could not be decrypted. The session was most likely "
                "encrypted with a different SESSION_ENCRYPTION_KEY."
            ) from exc

    @staticmethod
    def fingerprint(session_string: str) -> str:
        """Stable, non-reversible fingerprint used for admin display/verification."""
        return hashlib.sha256((session_string or "").encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")
