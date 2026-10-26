"""Encrypted session storage (persistent volume first, MongoDB as backup)."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.crypto import SessionCipher, SessionDecryptError
from app.core.redact import mask_phone
from app.db.models import utcnow
from app.db.repositories import Repositories

logger = logging.getLogger(__name__)

SESSION_FILENAME = "session.enc"
META_FILENAME = "meta.json"


class OwnershipError(PermissionError):
    """Raised when a user tries to touch another user's session."""


@dataclass
class StoredSession:
    user_id: int
    session_string: str
    fingerprint: str
    source: str  # "volume" | "mongo"


class SessionStore:
    """Owns everything about where session data lives on disk.

    Layout::

        <sessions_dir>/<user_id>/session.enc   # Fernet ciphertext, mode 0600
        <sessions_dir>/<user_id>/meta.json     # non-sensitive metadata

    The ciphertext is *also* written to MongoDB as a backup. Neither copy is
    usable without the Fernet key (``SESSION_ENCRYPTION_KEY`` or the auto-created
    ``<DATA_DIR>/session.key``); see the README for the security model, backup
    and revocation guidance.
    """

    def __init__(self, root: Path, cipher: SessionCipher, repos: Repositories) -> None:
        self._root = Path(root)
        self._cipher = cipher
        self._repos = repos
        self._root.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def user_dir(self, user_id: int) -> Path:
        return self._root / str(int(user_id))

    def session_file(self, user_id: int) -> Path:
        return self.user_dir(user_id) / SESSION_FILENAME

    # -- ownership ---------------------------------------------------------
    @staticmethod
    def assert_owner(owner_id: int, requester_id: int, *, is_admin: bool = False) -> None:
        if int(owner_id) == int(requester_id):
            return
        if is_admin:
            return
        raise OwnershipError(
            f"user {requester_id} attempted to access the session of {owner_id}"
        )

    # -- writes ------------------------------------------------------------
    async def save(
        self,
        user_id: int,
        session_string: str,
        *,
        tg_user_id: Optional[int] = None,
        dc_id: Optional[int] = None,
        phone_masked: Optional[str] = None,
    ) -> str:
        """Encrypt and persist a session. Returns the fingerprint."""
        if not session_string:
            raise ValueError("refusing to store an empty session string")
        fingerprint = SessionCipher.fingerprint(session_string)
        token = self._cipher.encrypt(session_string)
        self._write_atomic(self.session_file(int(user_id)), token)
        self._write_meta(
            int(user_id),
            {
                "tg_user_id": tg_user_id,
                "dc_id": dc_id,
                "phone_masked": phone_masked or None,
                "fingerprint": fingerprint,
                "updated_at": utcnow().isoformat(),
            },
        )
        await self._repos.sessions.upsert(
            int(user_id),
            token,
            fingerprint=fingerprint,
            tg_user_id=tg_user_id,
            dc_id=dc_id,
            phone_masked=phone_masked,
        )
        logger.info("session stored for user %s (fingerprint=%s)", user_id, fingerprint)
        return fingerprint

    async def load(self, user_id: int, *, requester_id: Optional[int] = None) -> Optional[StoredSession]:
        """Load and decrypt a session.

        The volume copy wins; MongoDB is used as a fallback (and re-hydrates the
        volume copy).
        """
        if requester_id is not None:
            self.assert_owner(user_id, requester_id)

        path = self.session_file(int(user_id))
        if path.exists():
            try:
                token = path.read_bytes()
                plaintext = self._cipher.decrypt(token, user_id=user_id)
                return StoredSession(
                    user_id=int(user_id),
                    session_string=plaintext,
                    fingerprint=SessionCipher.fingerprint(plaintext),
                    source="volume",
                )
            except (OSError, SessionDecryptError) as exc:
                logger.error("volume session unusable for user %s: %s", user_id, exc)

        token = await self._repos.sessions.get_encrypted(int(user_id))
        if token is None:
            return None
        try:
            plaintext = self._cipher.decrypt(token, user_id=user_id)
        except SessionDecryptError:
            logger.error("stored session for user %s cannot be decrypted", user_id)
            return None
        self._write_atomic(path, token)
        return StoredSession(
            user_id=int(user_id),
            session_string=plaintext,
            fingerprint=SessionCipher.fingerprint(plaintext),
            source="mongo",
        )

    async def load_session_string(self, user_id: int) -> Optional[str]:
        stored = await self.load(user_id)
        return stored.session_string if stored else None

    async def delete(self, user_id: int, *, requester_id: Optional[int] = None) -> bool:
        """Securely remove a session from the volume and from MongoDB."""
        if requester_id is not None:
            self.assert_owner(user_id, requester_id)
        removed = await self._repos.sessions.delete(int(user_id))
        directory = self.user_dir(int(user_id))
        local_removed = self._secure_remove_dir(directory)
        if directory.exists():
            raise OSError(f"session directory for user {user_id} could not be removed")
        logger.info("session deleted for user %s", user_id)
        return removed or local_removed

    async def exists(self, user_id: int) -> bool:
        if self.session_file(int(user_id)).exists():
            return True
        return await self._repos.sessions.get_encrypted(int(user_id)) is not None

    # -- maintenance -------------------------------------------------------
    def list_local_sessions(self) -> List[int]:
        found: List[int] = []
        for child in self._root.iterdir():
            if child.is_dir() and child.name.isdigit():
                found.append(int(child.name))
        return found

    def prune_unknown(self, known_user_ids: set[int]) -> List[int]:
        """Delete local session dirs whose owner no longer exists in MongoDB."""
        removed: List[int] = []
        for user_id in self.list_local_sessions():
            if user_id not in known_user_ids:
                self._secure_remove_dir(self.user_dir(user_id))
                removed.append(user_id)
        return removed

    def metadata(self, user_id: int) -> Dict[str, Any]:
        import json

        path = self.user_dir(int(user_id)) / META_FILENAME
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    # -- internals ---------------------------------------------------------
    def _write_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def _write_meta(self, user_id: int, payload: Dict[str, Any]) -> None:
        import json

        directory = self.user_dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / META_FILENAME
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def _secure_remove_dir(self, directory: Path) -> bool:
        """Overwrite file contents before unlinking, then drop the directory."""
        if not directory.exists():
            return False
        for file in directory.rglob("*"):
            if file.is_file():
                try:
                    size = file.stat().st_size
                    with open(file, "r+b") as handle:
                        handle.write(b"\x00" * min(size, 1 << 20))
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError:  # pragma: no cover
                    pass
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            logger.error("could not remove session directory %s", directory)
            return False
        return True


def masked_phone_of(phone: Optional[str]) -> Optional[str]:
    return mask_phone(phone) if phone else None


def parse_datetime(value: Any) -> Optional[datetime]:  # pragma: no cover - helper
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return None
    return None
