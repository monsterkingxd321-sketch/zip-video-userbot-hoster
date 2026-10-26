"""Secret redaction helpers.

Every log record produced by the application passes through :class:`Redactor`
so that tokens, API hashes, session strings, MongoDB credentials, phone numbers
and any ``key=value`` style secret can never reach the logs.
"""

from __future__ import annotations

import re
from typing import Iterable, Pattern, Tuple

REDACTED = "<redacted>"

# Order matters: the most specific / longest patterns must run first.
DEFAULT_PATTERNS: Tuple[Tuple[Pattern[str], str], ...] = (
    # Telethon / Pyrogram string sessions are long base64-ish blobs.
    (re.compile(r"\b\d[A-Za-z0-9_\-]{200,}\b"), "<redacted:session>"),
    # Fernet keys (44 char urlsafe base64 ending in '=').
    (re.compile(r"\b[A-Za-z0-9_\-]{43}="), "<redacted:encryption-key>"),
    # Bot API tokens: 8-10 digits, colon, 30+ chars.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{25,}\b"), "<redacted:bot-token>"),
    # MongoDB URIs with inline credentials.
    (
        re.compile(r"(?i)\b(mongodb(?:\+srv)?://)([^:@/\s]+):([^@/\s]+)@"),
        r"\1<redacted>:<redacted>@",
    ),
    # Telegram api_hash / api_id pairs and raw 32 char hex hashes.
    (re.compile(r"(?i)\b[0-9a-f]{32}\b"), "<redacted:api-hash>"),
    # phone numbers in international format.
    (re.compile(r"(?<![\w.])\+\d[\d\s\-()]{6,18}\d"), "<redacted:phone>"),
    # key=value / key: value secrets.
    (
        re.compile(
            r"(?i)\b(otp|one[_\s-]?time[_\s-]?code|login[_\s-]?code|code|password|passwd|pwd|"
            r"api_hash|bot_token|session_string|token|secret)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1=<redacted>",
    ),
)


class Redactor:
    """Applies a set of regex substitutions to arbitrary text."""

    def __init__(self, patterns: Iterable[Tuple[Pattern[str], str]] = DEFAULT_PATTERNS):
        self._patterns = tuple(patterns)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text

    def add_pattern(self, pattern: Pattern[str], replacement: str) -> None:
        self._patterns = self._patterns + ((pattern, replacement),)


def mask_phone(phone: str) -> str:
    """Return a display-safe phone number, e.g. ``+9193***21``."""
    digits = re.sub(r"[^\d+]", "", phone or "")
    if len(digits) < 6:
        return REDACTED
    return f"{digits[:5]}{'*' * 3}{digits[-2:]}"


def mask_id(value: object) -> str:
    """Return a short fingerprint of an opaque identifier."""
    text = str(value or "")
    if len(text) <= 8:
        return text or REDACTED
    return f"{text[:4]}...{text[-4:]}"


def redact_mongo_uri(uri: str) -> str:
    """Strip credentials out of a MongoDB URI for display."""
    return Redactor().redact(uri or "")


default_redactor = Redactor()
