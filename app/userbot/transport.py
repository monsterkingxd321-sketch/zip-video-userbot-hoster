"""Transport abstraction over a user-account MTProto client.

The pipeline, scheduler and login manager only ever talk to this protocol, which
is what allows the whole delivery path to be unit tested without real Telegram
credentials (see ``tests/fakes.py``).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence, runtime_checkable

from app.zipengine.probe import VideoMetadata

ProgressCallback = Callable[[int, Optional[int]], None]
CancelCheck = Callable[[], bool]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TransportError(Exception):
    """Base class for transport level failures."""


class TransientTransportError(TransportError):
    """Retryable: network blips, timeouts, server hiccups."""


class UncertainSendError(TransientTransportError):
    """The request may have reached Telegram; retrying can duplicate content.

    Callers must prefer a deterministic ``random_id`` retry, and give up if the
    de-duplication window may have expired.
    """


class PermanentTransportError(TransportError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class AccountRevokedError(PermanentTransportError):
    """Session is no longer valid (revoked, logged out, banned, deactivated)."""

    def __init__(self, code: str = "SESSION_REVOKED", message: str = "") -> None:
        super().__init__(code, message)


class FloodWaitSignal(TransportError):
    """Telegram asked us to wait; carries the exact number of seconds."""

    def __init__(self, seconds: float, *, method: str = "") -> None:
        super().__init__(f"FloodWait {seconds}s on {method or 'request'}")
        self.seconds = float(seconds)
        self.method = method


class PasswordRequiredSignal(TransportError):
    """Two-step verification is enabled for this account."""


class OperationCancelled(TransportError):
    """The local caller cancelled the operation before it completed."""


# RPC error codes that mean "this session can never work again".
REVOKED_ERROR_CODES = frozenset(
    {
        "AUTH_KEY_UNREGISTERED",
        "AUTH_KEY_DUPLICATED",
        "AUTH_KEY_INVALID",
        "SESSION_REVOKED",
        "SESSION_EXPIRED",
        "USER_DEACTIVATED",
        "USER_DEACTIVATED_BAN",
    }
)

# RPC error codes that are safe to retry with backoff.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "SERVER_ERROR",
        "TIMED_OUT",
        "TIMEOUT",
        "INTERDC_CALL_ERROR",
        "RPC_CALL_FAIL",
        "RPC_MCGET_FAIL",
        "WORKER_BUSY_TOO_LONG_RETRY",
        "NETWORK_MIGRATE",
        "PHONE_MIGRATE",
        "FILE_MIGRATE",
        "MSG_WAIT_FAILED",
        "MSG_WAIT_TIMEOUT",
        "CONNECTION_LAYER_INVALID",
        "HISTORY_GET_FAILED",
        "FILE_REFERENCE_EXPIRED",
        "FILE_REFERENCE_INVALID",
    }
)


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountInfo:
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.user_id))


@dataclass(frozen=True)
class CodeRequestResult:
    phone_code_hash: str
    next_type: Optional[str] = None
    timeout: Optional[int] = None


@dataclass
class MediaDescriptor:
    message_id: int
    chat_id: int
    has_document: bool = False
    has_video: bool = False
    has_photo: bool = False
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: int = 0
    raw: Any = None

    @property
    def extension(self) -> str:
        return posixpath.splitext((self.file_name or "").lower())[1]

    @property
    def is_zip(self) -> bool:
        if self.extension == ".zip":
            return True
        return bool(self.mime_type and "zip" in self.mime_type.lower())


@dataclass(frozen=True)
class CommandContext:
    """A command observed by a connected user account."""

    chat_id: int
    message_id: int
    sender_id: int
    text: str
    peer_type: str = "user"
    is_private: bool = True
    is_outgoing: bool = True
    reply: Optional[MediaDescriptor] = None
    is_self: bool = False

    @property
    def peer_kind(self) -> str:
        return "self" if self.is_self else self.peer_type


@dataclass
class SendOutcome:
    message_id: Optional[int] = None
    bytes_sent: int = 0
    delivered_as: str = "video"


@dataclass
class CommandHandlers:
    """Callbacks a transport must invoke for userbot commands."""

    on_command: Callable[[CommandContext], Awaitable[None]]
    command_prefixes: Sequence[str] = ("/unzip", "/uz", "/cancel")

    def matches(self, text: Optional[str]) -> bool:
        if not text:
            return False
        head = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        return any(head == prefix for prefix in self.command_prefixes)


@dataclass
class TransportStats:
    downloads: int = 0
    uploads: int = 0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    errors: int = 0
    flood_waits: int = 0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class UserbotTransport(Protocol):
    """Everything the platform needs from a Telegram user account."""

    user_id: int

    # -- lifecycle --------------------------------------------------------
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_authorized(self) -> bool: ...

    async def get_account(self) -> Optional[AccountInfo]: ...

    def session_string(self) -> str: ...

    def dc_id(self) -> Optional[int]: ...

    # -- login flow -------------------------------------------------------
    async def send_code_request(self, phone: str) -> CodeRequestResult: ...

    async def sign_in_with_code(self, phone: str, code: str, phone_code_hash: str) -> AccountInfo: ...

    async def sign_in_with_password(self, password: str) -> AccountInfo: ...

    # -- commands ---------------------------------------------------------
    def register_commands(self, handlers: CommandHandlers) -> None: ...

    # -- peer resolution --------------------------------------------------
    async def peer_spec_for(self, chat_id: int, peer_type: str = "user") -> Optional[dict]:
        """Describe a peer in a form that survives a restart.

        The result is persisted with the task so that a recovered job can reach
        the same chat without a live entity lookup.
        """
        ...

    # -- tasks ------------------------------------------------------------
    async def get_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> Optional[MediaDescriptor]: ...

    async def download_media(
        self,
        media: MediaDescriptor,
        destination: Path,
        *,
        progress: Optional[ProgressCallback] = None,
        cancel_check: Optional[CancelCheck] = None,
        chunk_size_kb: int = 512,
        max_bytes: Optional[int] = None,
    ) -> int: ...

    async def send_video(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        metadata: Optional[VideoMetadata] = None,
        spoiler: bool = False,
        supports_streaming: bool = True,
        random_id: int = 0,
        reply_to: Optional[int] = None,
        progress: Optional[ProgressCallback] = None,
        cancel_check: Optional[CancelCheck] = None,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> SendOutcome: ...

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> int: ...

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> None: ...

    async def delete_messages(
        self,
        chat_id: int,
        message_ids: Sequence[int],
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> None: ...
