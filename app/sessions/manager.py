"""Session lifecycle: login, encrypted persistence, transport creation, deletion.

Security notes
--------------
* OTPs and two-step passwords never leave the coroutine that received them and
  are never written to MongoDB, disk or the logs (see
  :class:`~app.sessions.login.LoginManager`).
* A session is stored as Fernet ciphertext on the persistent volume **and** as a
  ciphertext backup in MongoDB. MongoDB is a *backup*, not the primary store:
  the volume copy is what a restarting worker reads first.
* Only masked phone numbers are ever persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.constants import AccountStatus
from app.core.metrics import Metrics, metrics as default_metrics
from app.db.repositories import Repositories
from app.sessions.login import CompletedLogin, LoginManager
from app.sessions.states import LoginResult
from app.sessions.store import SessionStore, masked_phone_of
from app.userbot.transport import (
    AccountInfo,
    AccountRevokedError,
    PermanentTransportError,
    TransientTransportError,
    UserbotTransport,
)

logger = logging.getLogger(__name__)


@dataclass
class ConnectedAccount:
    transport: UserbotTransport
    account: AccountInfo
    fingerprint: str
    source: str


class SessionManager:
    """Owns the login flow and every operation on stored sessions."""

    def __init__(
        self,
        *,
        settings: Any,
        repos: Repositories,
        store: SessionStore,
        transport_factory: Callable[[int, str], UserbotTransport],
        metrics: Optional[Metrics] = None,
        login_ttl_seconds: float = 900.0,
    ) -> None:
        self._settings = settings
        self._repos = repos
        self._store = store
        self._transport_factory = transport_factory
        self._metrics = metrics or default_metrics
        self.login = LoginManager(
            transport_factory=transport_factory,
            ttl_seconds=login_ttl_seconds,
            max_code_attempts=3,
            max_password_attempts=3,
            require_same_account=bool(settings.require_same_account),
            metrics=self._metrics,
        )

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------
    async def start_login(self, user_id: int, phone: str) -> LoginResult:
        user = await self._repos.users.get(int(user_id))
        expected = None
        if user is not None:
            stored = (user.account or {}).get("user_id")
            if stored is not None:
                try:
                    expected = int(stored)
                except (TypeError, ValueError):
                    expected = None
        result = await self.login.begin(int(user_id), phone, expected_account_id=expected)
        if result.outcome.value == "code_sent":
            await self._repos.users.set_status(user_id, AccountStatus.AWAITING_CODE.value)
            await self._repos.audit.record(
                user_id, "login.code_requested", target_user_id=user_id
            )
        return result

    async def submit_code(self, user_id: int, code: str) -> LoginResult:
        result = await self.login.submit_code(int(user_id), code)
        await self._sync_status_from_login(user_id, result)
        return result

    async def submit_password(self, user_id: int, password: str) -> LoginResult:
        result = await self.login.submit_password(int(user_id), password)
        await self._sync_status_from_login(user_id, result)
        return result

    async def abandon_login(self, user_id: int) -> None:
        await self.login.abandon(int(user_id))

    async def login_state(self, user_id: int):
        return self.login.state_of(int(user_id))

    async def cleanup_expired_logins(self) -> int:
        return await self.login.cleanup_expired()

    async def finalize_login(self, user_id: int) -> Optional[CompletedLogin]:
        """Persist a completed login (encrypted) and mark the account connected."""
        completed = self.login.take_completed(int(user_id))
        if completed is None:
            return None

        account = completed.account
        expected = completed.expected_account_id
        if (
            self._settings.require_same_account
            and expected is not None
            and int(account.user_id) != int(expected)
        ):
            logger.warning("refusing to store a session that belongs to another account")
            self._metrics.incr("session.account_mismatch")
            return None

        phone_masked = masked_phone_of(account.phone)
        fingerprint = await self._store.save(
            user_id,
            completed.session_string,
            tg_user_id=int(account.user_id),
            dc_id=completed.dc_id,
            phone_masked=phone_masked,
        )
        await self._repos.users.set_account(
            user_id,
            {
                "user_id": int(account.user_id),
                "username": account.username,
                "first_name": account.first_name,
                "last_name": account.last_name,
                "phone_masked": phone_masked,
                "fingerprint": fingerprint,
                "dc_id": completed.dc_id,
            },
        )
        await self._repos.users.set_status(user_id, AccountStatus.CONNECTED.value)
        await self._repos.audit.record(
            user_id,
            "session.connected",
            target_user_id=user_id,
            detail={"fingerprint": fingerprint},
        )
        self._metrics.incr("session.connected")
        logger.info("session activated for user %s (fingerprint=%s)", user_id, fingerprint)
        return completed

    # ------------------------------------------------------------------
    # transports
    # ------------------------------------------------------------------
    async def create_transport(
        self, user_id: int, *, requester_id: Optional[int] = None
    ) -> Optional[ConnectedAccount]:
        """Load the stored session and return a connected, authorized transport."""
        user = await self._repos.users.get(int(user_id))
        if (
            user is None
            or user.is_banned
            or user.status != AccountStatus.CONNECTED.value
        ):
            return None
        stored = await self._store.load(int(user_id), requester_id=requester_id)
        if stored is None:
            return None

        transport = self._transport_factory(int(user_id), stored.session_string)
        try:
            await transport.connect()
            if not await transport.is_authorized():
                await self._safe_disconnect(transport)
                await self._mark_revoked(user_id, "the stored session is no longer authorized")
                return None
            account = await transport.get_account()
        except AccountRevokedError:
            await self._safe_disconnect(transport)
            await self._mark_revoked(user_id, "Telegram revoked this session")
            return None
        except (TransientTransportError, PermanentTransportError) as exc:
            await self._safe_disconnect(transport)
            logger.warning("could not start userbot for %s: %s", user_id, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            await self._safe_disconnect(transport)
            logger.warning("could not start userbot for %s: %s", user_id, type(exc).__name__)
            return None

        if account is None:
            await self._safe_disconnect(transport)
            return None
        return ConnectedAccount(
            transport=transport,
            account=account,
            fingerprint=stored.fingerprint,
            source=stored.source,
        )

    async def delete_session(
        self, user_id: int, *, requester_id: Optional[int] = None
    ) -> bool:
        removed = await self._store.delete(int(user_id), requester_id=requester_id)
        await self._repos.users.clear_account(user_id)
        await self._repos.users.set_status(user_id, AccountStatus.DISCONNECTED.value)
        await self._repos.audit.record(
            user_id, "session.deleted", target_user_id=user_id
        )
        self._metrics.incr("session.deleted")
        return removed

    async def has_session(self, user_id: int) -> bool:
        return await self._store.exists(int(user_id))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _sync_status_from_login(self, user_id: int, result: LoginResult) -> None:
        from app.sessions.states import LoginOutcome, LoginState

        if result.outcome is LoginOutcome.NO_PENDING_LOGIN:
            user = await self._repos.users.get(user_id)
            if user is not None and user.status in (
                AccountStatus.AWAITING_CODE.value,
                AccountStatus.AWAITING_PASSWORD.value,
            ):
                await self._repos.users.set_status(user_id, AccountStatus.NEW.value)
            return
        if result.state is LoginState.AWAITING_CODE:
            await self._repos.users.set_status(user_id, AccountStatus.AWAITING_CODE.value)
        elif result.state is LoginState.AWAITING_PASSWORD:
            await self._repos.users.set_status(user_id, AccountStatus.AWAITING_PASSWORD.value)
        elif result.state is LoginState.FAILED:
            await self._repos.users.set_status(user_id, AccountStatus.ERROR.value)

    async def _mark_revoked(self, user_id: int, reason: str) -> None:
        await self._repos.users.set_status(user_id, AccountStatus.REVOKED.value)
        await self._repos.audit.record(
            user_id, "session.revoked", target_user_id=user_id, detail={"reason": reason}
        )
        self._metrics.incr("session.revoked")
        logger.warning("session for user %s is no longer usable: %s", user_id, reason)

    @staticmethod
    async def _safe_disconnect(transport: UserbotTransport) -> None:
        try:
            await transport.disconnect()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("disconnect failed: %s", type(exc).__name__)


__all__ = ["SessionManager", "ConnectedAccount"]
