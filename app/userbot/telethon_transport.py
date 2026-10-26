"""Telethon implementation of :class:`~app.userbot.transport.UserbotTransport`.

Everything Telethon-specific lives here so the rest of the application can be
tested with a fake transport.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import aiofiles
from telethon import TelegramClient, events, functions, types
from telethon import errors as tg_errors
from telethon.errors import (
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.constants import EXTENSION_TO_MIME
from app.userbot.transport import (
    REVOKED_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    AccountInfo,
    AccountRevokedError,
    CodeRequestResult,
    CommandContext,
    CommandHandlers,
    FloodWaitSignal,
    MediaDescriptor,
    OperationCancelled,
    PasswordRequiredSignal,
    PermanentTransportError,
    SendOutcome,
    TransientTransportError,
    UncertainSendError,
    UserbotTransport,
)
from app.zipengine.probe import VideoMetadata

logger = logging.getLogger(__name__)

MAX_PART_SIZE_KB = 512  # Telethon refuses larger chunks


class _DownloadCancelled(Exception):
    """Internal signal raised from inside Telethon's progress callback."""


def error_code(exc: BaseException) -> str:
    """Derive a stable SCREAMING_SNAKE code from a Telethon RPC error."""
    if isinstance(exc, RPCError):
        raw = getattr(exc, "message", None)
        if isinstance(raw, str) and raw.isupper() and "_" in raw:
            return raw
    name = type(exc).__name__
    if name.endswith("Error"):
        name = name[:-5]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def map_exception(exc: BaseException, *, method: str) -> BaseException:
    """Translate Telethon exceptions into the transport error taxonomy."""
    if isinstance(exc, FloodWaitError):
        return FloodWaitSignal(getattr(exc, "seconds", 0) or 0, method=method)
    if isinstance(exc, tg_errors.FloodPremiumWaitError):  # type: ignore[attr-defined]
        return FloodWaitSignal(getattr(exc, "seconds", 0) or 0, method=method)
    if isinstance(exc, SessionPasswordNeededError):
        return PasswordRequiredSignal("two-step verification is enabled")

    if isinstance(exc, RPCError):
        code = error_code(exc)
        if code in REVOKED_ERROR_CODES:
            return AccountRevokedError(code, f"{type(exc).__name__}")
        if code in TRANSIENT_ERROR_CODES or isinstance(exc, tg_errors.ServerError):
            return TransientTransportError(f"{method}: {code}")
        if code in ("FLOOD_PREMIUM_WAIT", "FLOOD_WAIT"):
            return FloodWaitSignal(0, method=method)
        return PermanentTransportError(code, f"{method}: {type(exc).__name__}")

    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return TransientTransportError(f"{method}: {type(exc).__name__}")
    return exc


class TelethonTransport(UserbotTransport):
    """A single Telegram user account, connected through MTProto."""

    def __init__(
        self,
        user_id: int,
        api_id: int,
        api_hash: str,
        session_string: str = "",
        *,
        device_model: str = "ZipUserbot",
        system_version: str = "Linux",
        app_version: str = "1.0.0",
        lang_code: str = "en",
        system_lang_code: str = "en-US",
        connection_retries: int = 5,
        retry_delay: int = 2,
        timeout: int = 30,
        request_retries: int = 4,
        upload_part_size_kb: int = 512,
        use_ipv6: bool = False,
    ) -> None:
        self.user_id = int(user_id)
        self._api_id = api_id
        self._api_hash = api_hash
        self._part_size_kb = max(1, min(MAX_PART_SIZE_KB, int(upload_part_size_kb)))
        self._client = TelegramClient(
            StringSession(session_string or None),
            api_id,
            api_hash,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            lang_code=lang_code,
            system_lang_code=system_lang_code,
            connection_retries=connection_retries,
            retry_delay=retry_delay,
            timeout=timeout,
            request_retries=request_retries,
            auto_reconnect=True,
            use_ipv6=use_ipv6,
            # Never let Telethon swallow FloodWaits: the scheduler owns backoff.
            flood_sleep_threshold=0,
        )
        self._handlers: Optional[CommandHandlers] = None
        self._connected = False
        self._self_id: Optional[int] = None

    # -- lifecycle --------------------------------------------------------
    @property
    def client(self) -> TelegramClient:
        return self._client

    async def connect(self) -> None:
        if self._connected:
            return
        await self._guard("connect", self._client.connect())
        self._connected = True

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("disconnect failed for user %s: %s", self.user_id, exc)
        finally:
            self._connected = False

    async def is_authorized(self) -> bool:
        try:
            return bool(await self._client.is_user_authorized())
        except Exception as exc:
            mapped = map_exception(exc, method="is_user_authorized")
            if isinstance(mapped, AccountRevokedError):
                return False
            raise mapped

    async def get_account(self) -> Optional[AccountInfo]:
        me = await self._guard("get_me", self._client.get_me())
        if me is None:
            return None
        self._self_id = int(me.id)
        return AccountInfo(
            user_id=int(me.id),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            last_name=getattr(me, "last_name", None),
            phone=getattr(me, "phone", None),
        )

    def session_string(self) -> str:
        return StringSession.save(self._client.session)

    def dc_id(self) -> Optional[int]:
        return getattr(self._client.session, "dc_id", None)

    # -- login ------------------------------------------------------------
    async def send_code_request(self, phone: str) -> CodeRequestResult:
        result = await self._guard(
            "send_code_request", self._client.send_code_request(phone, force_sms=False)
        )
        return CodeRequestResult(
            phone_code_hash=result.phone_code_hash,
            next_type=getattr(getattr(result, "next_type", None), "__class__", None)
            and type(result.next_type).__name__,
            timeout=getattr(result, "timeout", None),
        )

    async def sign_in_with_code(self, phone: str, code: str, phone_code_hash: str) -> AccountInfo:
        await self._guard(
            "sign_in",
            self._client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash),
        )
        account = await self.get_account()
        if account is None:
            raise PermanentTransportError("SIGN_IN_FAILED", "Telegram did not return an account")
        return account

    async def sign_in_with_password(self, password: str) -> AccountInfo:
        await self._guard("sign_in_password", self._client.sign_in(password=password))
        account = await self.get_account()
        if account is None:
            raise PermanentTransportError("SIGN_IN_FAILED", "Telegram did not return an account")
        return account

    # -- command wiring ---------------------------------------------------
    def register_commands(self, handlers: CommandHandlers) -> None:
        self._handlers = handlers

        @self._client.on(
            events.NewMessage(
                incoming=False,
                outgoing=True,
                func=lambda event: handlers.matches(event.raw_text),
            )
        )
        async def _on_command(event) -> None:  # pragma: no cover - exercised live
            # Defence in depth: only commands sent by the connected account
            # itself are allowed. Incoming messages from other group members
            # must never trigger actions on this account.
            if not bool(getattr(event.message, "out", False)):
                return
            sender = getattr(event, "sender_id", None)
            if self._self_id is not None and sender is not None and int(sender) != self._self_id:
                logger.warning(
                    "ignoring userbot command from unexpected sender %s on account %s",
                    sender,
                    self.user_id,
                )
                return
            try:
                context = await self.build_command_context(event)
            except Exception as exc:
                logger.warning("failed to build command context: %s", type(exc).__name__)
                return
            await handlers.on_command(context)

    async def build_command_context(self, event) -> CommandContext:
        message = event.message
        chat = await event.get_chat()
        sender_id = getattr(event, "sender_id", None) or getattr(chat, "id", 0)
        peer_type, is_self = _classify_peer(chat)
        reply: Optional[MediaDescriptor] = None
        if message.reply_to_msg_id:
            replied = await self._guard(
                "get_messages",
                self._client.get_messages(chat, ids=message.reply_to_msg_id),
            )
            if replied is not None:
                reply = self._describe(replied, chat_id=int(chat.id))
        return CommandContext(
            chat_id=int(chat.id),
            message_id=int(message.id),
            sender_id=int(sender_id or 0),
            text=message.raw_text or "",
            peer_type=peer_type,
            is_private=bool(getattr(event, "is_private", False)),
            is_outgoing=bool(getattr(message, "out", False)),
            reply=reply,
            is_self=is_self,
        )

    # -- tasks ------------------------------------------------------------
    async def peer_spec_for(self, chat_id: int, peer_type: str = "user") -> Optional[dict]:
        if peer_type == "self" or chat_id in (0, "me"):
            return {"type": "InputPeerSelf", "access_hash": None}
        try:
            entity = await self._guard("get_input_entity", self._client.get_input_entity(chat_id))
        except Exception as exc:
            logger.debug("could not describe peer %s: %s", chat_id, type(exc).__name__)
            return None
        return self.peer_spec(entity)

    def peer_spec(self, chat: Any) -> Optional[dict]:
        """Build a resumable peer description from a Telethon entity."""
        peer = _input_peer_of(chat)
        if peer is None:
            return None
        payload: dict = {"type": peer.__class__.__name__, "access_hash": None}
        if isinstance(peer, types.InputPeerChannel):
            payload["channel_id"] = peer.channel_id
            payload["access_hash"] = peer.access_hash
        elif isinstance(peer, types.InputPeerUser):
            payload["user_id"] = peer.user_id
            payload["access_hash"] = peer.access_hash
        elif isinstance(peer, types.InputPeerChat):
            payload["chat_id"] = peer.chat_id
        return payload

    async def _resolve_peer(self, chat_id: int, peer_type: str, peer: Optional[dict]) -> Any:
        if peer:
            kind = str(peer.get("type") or "")
            try:
                if "Channel" in kind and peer.get("access_hash") is not None:
                    return types.InputPeerChannel(
                        channel_id=int(peer.get("channel_id") or chat_id),
                        access_hash=int(peer["access_hash"]),
                    )
                if "User" in kind and peer.get("access_hash") is not None:
                    return types.InputPeerUser(
                        user_id=int(peer.get("user_id") or chat_id),
                        access_hash=int(peer["access_hash"]),
                    )
                if "Chat" in kind:
                    return types.InputPeerChat(chat_id=int(peer.get("chat_id") or chat_id))
                if kind == "InputPeerSelf":
                    return types.InputPeerSelf()
            except (TypeError, ValueError):
                pass
        if peer_type == "self" or chat_id in (0, "me"):
            return types.InputPeerSelf()
        return await self._guard("get_input_entity", self._client.get_input_entity(chat_id))

    @staticmethod
    def _describe(message: Any, *, chat_id: int) -> MediaDescriptor:
        document = getattr(message, "document", None)
        file_name = None
        size = 0
        mime_type = None
        if document is not None:
            size = int(getattr(document, "size", 0) or 0)
            mime_type = getattr(document, "mime_type", None)
            for attribute in getattr(document, "attributes", ()) or ():
                if isinstance(attribute, types.DocumentAttributeFilename):
                    file_name = attribute.file_name
                    break
        return MediaDescriptor(
            message_id=int(message.id),
            chat_id=int(chat_id),
            has_document=document is not None,
            has_video=bool(getattr(message, "video", None) is not None),
            has_photo=bool(getattr(message, "photo", None) is not None),
            file_name=file_name,
            mime_type=mime_type,
            size=size,
            raw=message,
        )

    async def get_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> Optional[MediaDescriptor]:
        entity = await self._resolve_peer(chat_id, peer_type, peer)
        message = await self._guard(
            "get_messages", self._client.get_messages(entity, ids=int(message_id))
        )
        if message is None:
            return None
        return self._describe(message, chat_id=chat_id)

    async def download_media(
        self,
        media: MediaDescriptor,
        destination: Path,
        *,
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        chunk_size_kb: int = 512,
        max_bytes: Optional[int] = None,
    ) -> int:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Download into a .part file and atomically replace only after the whole
        # file is complete. A retry therefore never leaves a truncated ZIP that
        # looks valid to the extractor.
        part = destination.with_name(destination.name + ".part")
        part.unlink(missing_ok=True)
        request_size = max(4096, min(MAX_PART_SIZE_KB * 1024, int(chunk_size_kb) * 1024))
        request_size -= request_size % 4096
        total = int(media.size or 0) or None
        received = 0

        try:
            for attempt in range(2):
                try:
                    raw = media.raw
                    if raw is None or attempt:
                        raw = await self._refetch_raw(media)
                    received = 0
                    async with aiofiles.open(part, "wb") as sink:
                        async for chunk in self._client.iter_download(
                            raw, request_size=request_size
                        ):
                            if cancel_check is not None and cancel_check():
                                raise _DownloadCancelled()
                            received += len(chunk)
                            if max_bytes is not None and received > int(max_bytes):
                                raise PermanentTransportError(
                                    "ARCHIVE_TOO_LARGE",
                                    f"download exceeded the configured limit of {max_bytes} bytes",
                                )
                            await sink.write(chunk)
                            if progress is not None:
                                progress(received, total)
                    break
                except FileReferenceExpiredError:
                    part.unlink(missing_ok=True)
                    if attempt:
                        raise
                    logger.info("file reference expired for user %s; refreshing", self.user_id)
        except _DownloadCancelled as exc:
            part.unlink(missing_ok=True)
            raise OperationCancelled("download cancelled") from exc
        except Exception as exc:
            part.unlink(missing_ok=True)
            mapped = map_exception(exc, method="download_media")
            if mapped is exc:
                raise
            raise mapped from exc

        if received <= 0:
            part.unlink(missing_ok=True)
            raise PermanentTransportError(
                "DOWNLOAD_UNAVAILABLE",
                "this file cannot be downloaded with the connected account",
            )
        os.replace(part, destination)
        return received

    async def _refetch_raw(self, media: MediaDescriptor) -> Any:
        """Refresh a Message/document after ``FILE_REFERENCE_EXPIRED``."""
        peer_type = "self" if self._self_id is not None and int(media.chat_id) == self._self_id else "user"
        entity = await self._resolve_peer(media.chat_id, peer_type, None)
        message = await self._guard(
            "get_messages", self._client.get_messages(entity, ids=int(media.message_id))
        )
        if message is None or getattr(message, "document", None) is None:
            raise PermanentTransportError(
                "MESSAGE_NOT_FOUND", "the replied message is no longer available"
            )
        return message

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
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        peer_type: str = "user",
        peer: Optional[dict] = None,
    ) -> SendOutcome:
        path = Path(path)
        size = path.stat().st_size
        display_name = filename or path.name
        display_name = _safe_file_name(display_name)

        def _progress(sent: int, total: int) -> None:
            if cancel_check is not None and cancel_check():
                raise _DownloadCancelled()
            if progress is not None:
                progress(int(sent), int(total) if total else None)

        extension = posixpath.splitext(display_name.lower())[1]
        mime_type = EXTENSION_TO_MIME.get(extension, "video/mp4")

        attributes: list[Any] = [
            types.DocumentAttributeVideo(
                duration=float(metadata.duration) if metadata else 0.0,
                w=int(metadata.width) if metadata else 0,
                h=int(metadata.height) if metadata else 0,
                round_message=False,
                supports_streaming=bool(supports_streaming),
                nosound=False,
            ),
            types.DocumentAttributeFilename(file_name=display_name),
        ]

        # The upload itself is safe to retry: nothing is delivered until
        # SendMediaRequest succeeds.
        try:
            handle = await self._guard(
                "upload_file",
                self._client.upload_file(
                    str(path),
                    part_size_kb=self._part_size_kb,
                    file_size=size,
                    file_name=display_name,
                    progress_callback=_progress,
                ),
            )
        except _DownloadCancelled as exc:
            raise OperationCancelled("upload cancelled") from exc

        entity = await self._resolve_peer(chat_id, peer_type, peer)
        if cancel_check is not None and cancel_check():
            raise OperationCancelled("upload cancelled")
        media = types.InputMediaUploadedDocument(
            file=handle,
            mime_type=mime_type,
            attributes=attributes,
            force_file=False,
            spoiler=True if spoiler else None,
        )
        request = functions.messages.SendMediaRequest(
            peer=entity,
            media=media,
            message=caption or "",
            random_id=int(random_id) or None,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=int(reply_to)) if reply_to else None,
        )
        try:
            result = await self._client(request)
        except Exception as exc:
            mapped = map_exception(exc, method="SendMediaRequest")
            if isinstance(mapped, TransientTransportError):
                # The request may or may not have been applied.
                raise UncertainSendError(str(mapped)) from exc
            raise mapped

        return SendOutcome(
            message_id=_extract_message_id(result),
            bytes_sent=size,
            delivered_as="video",
        )

    async def send_text(self, chat_id: int, text: str, *, reply_to: Optional[int] = None,
                        peer_type: str = "user", peer: Optional[dict] = None) -> int:
        entity = await self._resolve_peer(chat_id, peer_type, peer)
        result = await self._guard(
            "send_message",
            self._client.send_message(
                entity,
                text,
                reply_to=int(reply_to) if reply_to else None,
                link_preview=False,
                parse_mode="html",
            ),
        )
        return int(getattr(result, "id", 0) or 0)

    async def edit_text(self, chat_id: int, message_id: int, text: str,
                        peer_type: str = "user", peer: Optional[dict] = None) -> None:
        entity = await self._resolve_peer(chat_id, peer_type, peer)
        try:
            await self._guard(
                "edit_message",
                self._client.edit_message(
                    entity, int(message_id), text, link_preview=False, parse_mode="html"
                ),
            )
        except PermanentTransportError as exc:
            if exc.code == "MESSAGE_NOT_MODIFIED":
                return
            raise

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int],
                              peer_type: str = "user", peer: Optional[dict] = None) -> None:
        if not message_ids:
            return
        entity = await self._resolve_peer(chat_id, peer_type, peer)
        await self._guard("delete_messages", self._client.delete_messages(entity, list(message_ids)))

    # -- helpers ----------------------------------------------------------
    async def _guard(self, method: str, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as exc:
            raise map_exception(exc, method=method) from exc


# ---------------------------------------------------------------------------
# Module level helpers
# ---------------------------------------------------------------------------
def _safe_file_name(name: str) -> str:
    """Keep a display filename safe and within Telegram's filename limit."""
    base = str(name or "video.mp4").replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in "\x00/\\")
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = "video.mp4"
    if len(cleaned) > 200:
        stem, extension = posixpath.splitext(cleaned)
        cleaned = stem[: max(1, 200 - len(extension))] + extension
    return cleaned


def _classify_peer(chat: Any) -> tuple[str, bool]:
    if isinstance(chat, types.InputPeerSelf):
        return "self", True
    name = type(chat).__name__
    if "Channel" in name:
        if getattr(chat, "megagroup", False):
            return "channel", False
        return "channel", False
    if "Chat" in name and "Channel" not in name:
        return "chat", False
    if "User" in name:
        if getattr(chat, "is_self", False):
            return "self", True
        return "user", False
    return "user", False


def _input_peer_of(entity: Any) -> Optional[Any]:
    if isinstance(entity, types.InputPeerSelf):
        return entity
    try:
        from telethon.utils import get_input_peer

        return get_input_peer(entity, check_hash=False)
    except Exception:
        return None


def _extract_message_id(result: Any) -> Optional[int]:
    """Pull the created message id out of an ``Updates`` response."""
    updates = getattr(result, "updates", None)
    if updates is None and hasattr(result, "id"):
        return int(result.id)
    for update in updates or ():
        message = getattr(update, "message", None)
        if message is not None and hasattr(message, "id"):
            return int(message.id)
    return None


__all__ = [
    "TelethonTransport",
    "error_code",
    "map_exception",
    "MAX_PART_SIZE_KB",
]
