"""Small in-memory transport doubles for tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.metrics import Metrics
from app.userbot.transport import (
    AccountInfo,
    CodeRequestResult,
    CommandHandlers,
    MediaDescriptor,
    OperationCancelled,
    SendOutcome,
    UncertainSendError,
    UserbotTransport,
)


class FakeTransport:
    """A deterministic transport that never touches the network."""

    def __init__(
        self,
        *,
        archive: bytes = b"",
        file_name: str = "archive.zip",
        user_id: int = 42,
        username: str = "tester",
        send_error: Optional[BaseException] = None,
    ) -> None:
        self.user_id = int(user_id)
        self.archive = archive
        self.file_name = file_name
        self.account = AccountInfo(user_id=user_id, username=username, first_name="Test")
        self.send_error = send_error
        self.connected = False
        self.downloads: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.texts: List[str] = []
        self.edits: List[str] = []
        self.handlers: Optional[CommandHandlers] = None
        self._message_counter = 100
        self.deleted: List[int] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_authorized(self) -> bool:
        return True

    async def get_account(self) -> AccountInfo:
        return self.account

    def session_string(self) -> str:
        return "1" + "A" * 300

    def dc_id(self) -> Optional[int]:
        return 2

    async def send_code_request(
        self, phone: str, *, force_sms: bool = False
    ) -> CodeRequestResult:
        return CodeRequestResult(
            phone_code_hash="hash",
            delivery="sms" if force_sms else "app",
            next_delivery="sms",
        )

    async def sign_in_with_code(self, phone: str, code: str, phone_code_hash: str) -> AccountInfo:
        return self.account

    async def sign_in_with_password(self, password: str) -> AccountInfo:
        return self.account

    def register_commands(self, handlers: CommandHandlers) -> None:
        self.handlers = handlers

    async def peer_spec_for(self, chat_id: int, peer_type: str = "user") -> Optional[dict]:
        return {"type": "InputPeerUser", "id": int(chat_id), "access_hash": 1}

    async def get_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> Optional[MediaDescriptor]:
        return MediaDescriptor(
            message_id=int(message_id),
            chat_id=int(chat_id),
            has_document=True,
            file_name=self.file_name,
            mime_type=(
                "application/zip" if str(self.file_name).lower().endswith(".zip") else "text/plain"
            ),
            size=len(self.archive),
            raw=object(),
        )

    async def download_media(
        self,
        media: MediaDescriptor,
        destination: Path,
        *,
        progress=None,
        cancel_check=None,
        chunk_size_kb: int = 512,
        max_bytes: Optional[int] = None,
    ) -> int:
        if cancel_check is not None and cancel_check():
            raise OperationCancelled("cancelled before download")
        if max_bytes is not None and len(self.archive) > max_bytes:
            raise ValueError("archive exceeds max_bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        chunk = max(1024, int(chunk_size_kb) * 1024)
        received = 0
        with open(destination, "wb") as handle:
            for offset in range(0, len(self.archive), chunk):
                if cancel_check is not None and cancel_check():
                    raise OperationCancelled("cancelled during download")
                piece = self.archive[offset : offset + chunk]
                handle.write(piece)
                received += len(piece)
                if progress is not None:
                    progress(received, len(self.archive))
        self.downloads.append({"destination": destination, "size": received})
        return received

    async def send_video(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        metadata=None,
        spoiler: bool = False,
        supports_streaming: bool = True,
        random_id: int = 0,
        reply_to: Optional[int] = None,
        progress=None,
        cancel_check=None,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> SendOutcome:
        if cancel_check is not None and cancel_check():
            raise OperationCancelled("cancelled before upload")
        if self.send_error is not None:
            raise self.send_error
        size = path.stat().st_size
        if progress is not None:
            progress(size, size)
        self._message_counter += 1
        self.sent.append(
            {
                "chat_id": int(chat_id),
                "path": Path(path),
                "filename": filename,
                "spoiler": bool(spoiler),
                "supports_streaming": bool(supports_streaming),
                "random_id": int(random_id),
                "bytes": size,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendOutcome(message_id=self._message_counter, bytes_sent=size)

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> int:
        self.texts.append(text)
        self._message_counter += 1
        return self._message_counter

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> None:
        self.edits.append(text)

    async def delete_messages(
        self,
        chat_id: int,
        message_ids,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> None:
        self.deleted.extend(int(mid) for mid in message_ids)

    def stats(self):
        return {
            "downloads": len(self.downloads),
            "uploads": len(self.sent),
        }


class FailingOnceTransport(FakeTransport):
    """Raise an uncertain send once, then succeed (for retry tests)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.failed_once = False

    async def send_video(self, *args, **kwargs):
        if not self.failed_once:
            self.failed_once = True
            raise UncertainSendError("test uncertainty")
        return await super().send_video(*args, **kwargs)


def assert_is_transport(transport: Any) -> None:
    assert isinstance(transport, UserbotTransport)
