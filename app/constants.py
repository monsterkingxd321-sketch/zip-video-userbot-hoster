"""Shared constants and small enums used across the application."""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# Video handling
# ---------------------------------------------------------------------------

# Extensions that we consider "videos" inside an archive.
DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".3gp",
    ".ogv",
    ".divx",
    ".vob",
)

# Containers Telegram is able to stream. Telegram rejects
# `supports_streaming=True` for other containers (VideoContentTypeInvalid
# error), so we only enable the flag for these.
DEFAULT_STREAMABLE_EXTENSIONS: tuple[str, ...] = (".mp4", ".m4v", ".mov")

EXTENSION_TO_MIME: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".3gp": "video/3gpp",
    ".ogv": "video/ogg",
    ".divx": "video/divx",
    ".vob": "video/dvd",
}

ZIP_EXTENSIONS: tuple[str, ...] = (".zip",)

# ---------------------------------------------------------------------------
# ZIP safety defaults (overridable through configuration)
# ---------------------------------------------------------------------------

DEFAULT_MAX_ARCHIVE_DEPTH = 3

# ---------------------------------------------------------------------------
# Task / account state machines
# ---------------------------------------------------------------------------

ACTIVE_TASK_STATUSES = ("queued", "downloading", "extracting", "sending")
TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled", "interrupted", "partial")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    SENDING = "sending"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class StepStatus(str, Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


class AccountStatus(str, Enum):
    NEW = "new"
    AWAITING_CODE = "awaiting_code"
    AWAITING_PASSWORD = "awaiting_password"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    ERROR = "error"


class UnzipMode(str, Enum):
    PLAIN = "plain"
    SPOILER = "spoiler"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

WORKER_HEARTBEAT_SECONDS = 30.0
TEMP_DIR_MAX_AGE_SECONDS = 24 * 3600
