"""Parsing of userbot commands (``/unzip``, ``/unzip spoiler``, ``/cancel``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from app.constants import UnzipMode

UNZIP_ALIASES = ("/unzip", "/uz")
CANCEL_ALIASES = ("/cancel",)

SPOILER_FLAGS = ("spoiler", "sp", "s")
RETRY_FLAGS = ("retry", "force", "again")


@dataclass(frozen=True)
class UnzipCommand:
    mode: str = UnzipMode.PLAIN.value
    retry: bool = False
    flags: Sequence[str] = field(default_factory=tuple)

    @property
    def spoiler(self) -> bool:
        return self.mode == UnzipMode.SPOILER.value


@dataclass(frozen=True)
class CancelCommand:
    scope: str = "latest"


def _head(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    return stripped.split()[0].split("@")[0].lower()


def _tokens(text: str) -> List[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    return [token.lower() for token in stripped.split()[1:]]


def is_unzip_command(text: Optional[str]) -> bool:
    return _head(text or "") in UNZIP_ALIASES


def is_cancel_command(text: Optional[str]) -> bool:
    return _head(text or "") in CANCEL_ALIASES


def parse_unzip_command(text: str, *, default_spoiler: bool = False) -> Optional[UnzipCommand]:
    """Parse ``/unzip [spoiler] [retry]``.

    Unknown flags are reported in ``flags`` so the caller can warn the user,
    but they never silently change behaviour.
    """
    if not is_unzip_command(text):
        return None
    tokens = _tokens(text)
    spoiler = bool(default_spoiler)
    retry = False
    unknown: List[str] = []
    for token in tokens:
        cleaned = token.strip().lower()
        if cleaned in SPOILER_FLAGS:
            spoiler = True
        elif cleaned in RETRY_FLAGS:
            retry = True
        elif cleaned == "nos" or cleaned == "nospoiler":
            spoiler = False
        else:
            unknown.append(cleaned)
    return UnzipCommand(
        mode=UnzipMode.SPOILER.value if spoiler else UnzipMode.PLAIN.value,
        retry=retry,
        flags=tuple(unknown),
    )


def parse_cancel_command(text: str) -> Optional[CancelCommand]:
    if not is_cancel_command(text):
        return None
    tokens = _tokens(text)
    scope = "latest"
    if tokens and tokens[0] in ("all", "everything"):
        scope = "all"
    elif tokens and tokens[0].isdigit():
        scope = tokens[0]
    return CancelCommand(scope=scope)
