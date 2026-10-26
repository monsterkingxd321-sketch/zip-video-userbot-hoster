"""Telegram login orchestration (official MTProto flow).

Security rules enforced here:

* OTPs and two-step passwords are used inside the coroutine that received them
  and are then dropped. They are never written to MongoDB, disk or logs.
* A pending login lives in memory only; a restart therefore requires the user to
  run ``/connect`` again (by design: nothing sensitive survives a restart).
* Phone numbers are only ever stored masked.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.core.metrics import Metrics, metrics as default_metrics
from app.sessions.states import (
    InvalidCodeError,
    InvalidPhoneError,
    LoginOutcome,
    LoginResult,
    LoginState,
    PendingLogin,
    normalize_login_code,
    normalize_phone,
)
from app.userbot.transport import (
    AccountInfo,
    AccountRevokedError,
    FloodWaitSignal,
    PasswordRequiredSignal,
    PermanentTransportError,
    TransientTransportError,
    TransportError,
    UserbotTransport,
)

logger = logging.getLogger(__name__)

# RPC codes -> user facing outcome
_CODE_INVALID = {"PHONE_CODE_INVALID", "PHONE_CODE_EMPTY"}
_CODE_EXPIRED = {"PHONE_CODE_EXPIRED", "PHONE_CODE_HASH_EMPTY", "PHONE_HASH_EXPIRED"}
_PHONE_INVALID = {"PHONE_NUMBER_INVALID", "PHONE_NUMBER_EMPTY"}
_PHONE_BANNED = {"PHONE_NUMBER_BANNED"}
_PHONE_UNOCCUPIED = {"PHONE_NUMBER_UNOCCUPIED"}
_SEND_CODE_UNAVAILABLE = {"SEND_CODE_UNAVAILABLE"}
_PASSWORD_INVALID = {"PASSWORD_HASH_INVALID"}
_API_CONFIG_CODES = {"API_ID_INVALID", "API_HASH_INVALID", "API_ID_PUBLISHED_FLOOD"}

_RPC_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


def _safe_rpc_code(value: str) -> str:
    """Keep only safe RPC identifiers in user-facing text and logs."""
    code = str(value or "").strip().upper()
    return code if _RPC_CODE_RE.fullmatch(code) else "UNKNOWN"


def _request_failure_result(
    code: str, *, state: LoginState = LoginState.IDLE
) -> LoginResult:
    """Turn a safe Telegram error code into an actionable message.

    The raw exception text is deliberately not used: it can change between
    Telethon versions and is not needed to help the user or the operator. Only
    a validated RPC code is shown, never a phone number, OTP or password.
    """
    code = _safe_rpc_code(code)
    if code in _API_CONFIG_CODES:
        return LoginResult(
            LoginOutcome.ERROR,
            state,
            message=(
                f"Telegram rejected the server's API credentials ({code}). "
                "The operator must set a real TELEGRAM_API_ID and TELEGRAM_API_HASH "
                "from my.telegram.org, then redeploy. Do not use the example values."
            ),
        )
    if code in _PHONE_INVALID:
        return LoginResult(
            LoginOutcome.INVALID_PHONE,
            state,
            message=(
                f"Telegram rejected this phone number ({code}). "
                "Include the country code, for example +919876543210."
            ),
        )
    if code in _PHONE_UNOCCUPIED:
        return LoginResult(
            LoginOutcome.INVALID_PHONE,
            state,
            message=(
                f"No Telegram account is registered with this number ({code}). "
                "Check the number and country code."
            ),
        )
    if code in _PHONE_BANNED:
        return LoginResult(
            LoginOutcome.ERROR,
            state,
            message=f"Telegram has banned this phone number ({code}).",
        )
    if code.startswith("PHONE_NUMBER_FLOOD"):
        return LoginResult(
            LoginOutcome.ERROR,
            state,
            message=(
                f"Too many code requests for this number ({code}). "
                "Wait and try again later; Telegram can block requests for up to 24 hours."
            ),
        )
    if code in _SEND_CODE_UNAVAILABLE:
        return LoginResult(
            LoginOutcome.ERROR,
            state,
            message=(
                f"Telegram cannot send a login code to this number right now ({code}). "
                "Try again later, use another number, or try the SMS option."
            ),
        )
    if code == "AUTH_RESTART":
        return LoginResult(
            LoginOutcome.ERROR,
            state,
            message=(
                "Telegram asked to restart the login (AUTH_RESTART). "
                "Start again with /connect."
            ),
        )
    return LoginResult(
        LoginOutcome.ERROR,
        state,
        message=f"Telegram rejected the login request ({code}).",
    )


@dataclass
class CompletedLogin:
    user_id: int
    session_string: str
    account: AccountInfo
    dc_id: Optional[int] = None
    expected_account_id: Optional[int] = None
    created_at: float = 0.0


class LoginManager:
    def __init__(
        self,
        *,
        transport_factory: Callable[[int, str], UserbotTransport],
        ttl_seconds: float = 900.0,
        max_code_attempts: int = 3,
        max_password_attempts: int = 3,
        require_same_account: bool = True,
        clock: Callable[[], float] = time.monotonic,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self._transport_factory = transport_factory
        self._ttl = float(ttl_seconds)
        self._max_code_attempts = max(1, int(max_code_attempts))
        self._max_password_attempts = max(1, int(max_password_attempts))
        self._require_same_account = bool(require_same_account)
        self._clock = clock
        self._metrics = metrics or default_metrics
        self._pending: Dict[int, PendingLogin] = {}
        self._completed: Dict[int, CompletedLogin] = {}

    # -- introspection -----------------------------------------------------
    def has_pending(self, user_id: int) -> bool:
        pending = self._pending.get(int(user_id))
        return bool(pending and not pending.expired(self._clock()))

    def state_of(self, user_id: int) -> LoginState:
        pending = self._pending.get(int(user_id))
        if pending is None:
            return LoginState.IDLE
        if pending.expired(self._clock()):
            return LoginState.EXPIRED
        return pending.state

    def pending_phone(self, user_id: int) -> Optional[str]:
        pending = self._pending.get(int(user_id))
        return pending.phone if pending else None

    def take_completed(self, user_id: int) -> Optional[CompletedLogin]:
        return self._completed.pop(int(user_id), None)

    # -- flow --------------------------------------------------------------
    async def begin(
        self,
        user_id: int,
        raw_phone: str,
        *,
        expected_account_id: Optional[int] = None,
    ) -> LoginResult:
        try:
            phone = normalize_phone(raw_phone)
        except InvalidPhoneError as exc:
            return LoginResult(
                LoginOutcome.INVALID_PHONE, LoginState.IDLE, message=str(exc)
            )

        await self.abandon(user_id)
        transport = self._transport_factory(int(user_id), "")
        try:
            await transport.connect()
            result = await transport.send_code_request(phone, force_sms=False)
        except FloodWaitSignal as exc:
            await self._safe_disconnect(transport)
            code = _safe_rpc_code(getattr(exc, "code", "") or "FLOOD_WAIT")
            if code in _API_CONFIG_CODES:
                logger.warning("code request blocked for user %s: %s", user_id, code)
                return _request_failure_result(code)
            return LoginResult(
                LoginOutcome.FLOOD_WAIT,
                LoginState.IDLE,
                message="Telegram is rate limiting code requests for this number.",
                retry_after=exc.seconds,
            )
        except PermanentTransportError as exc:
            await self._safe_disconnect(transport)
            code = _safe_rpc_code(exc.code)
            logger.warning("code request rejected for user %s: %s", user_id, code)
            return _request_failure_result(code)
        except Exception as exc:  # pragma: no cover - network dependent
            await self._safe_disconnect(transport)
            logger.warning("code request failed for user %s: %s", user_id, type(exc).__name__)
            return LoginResult(
                LoginOutcome.ERROR,
                LoginState.IDLE,
                message="Could not reach Telegram. Please try again.",
            )

        self._pending[int(user_id)] = PendingLogin(
            user_id=int(user_id),
            phone=phone,
            phone_code_hash=result.phone_code_hash,
            state=LoginState.AWAITING_CODE,
            delivery=result.delivery,
            transport=transport,
            created_at=self._clock(),
            ttl_seconds=self._ttl,
            expected_account_id=(
                int(expected_account_id) if expected_account_id is not None else None
            ),
        )
        self._metrics.incr("login.code_requested")
        logger.info("login code requested for user %s", user_id)
        return LoginResult(
            LoginOutcome.CODE_SENT,
            LoginState.AWAITING_CODE,
            message="Login code requested.",
            attempts_left=self._max_code_attempts,
            delivery=result.delivery,
            next_delivery=result.next_delivery,
        )

    async def resend_code(self, user_id: int, *, force_sms: bool = False) -> LoginResult:
        """Request another login code for the current pending login.

        ``force_sms`` asks Telegram to deliver by SMS instead of the Telegram
        app. Telegram may refuse or rate-limit the request; the returned result
        keeps that distinction so the UI can explain it.
        """
        pending = self._live_pending(user_id)
        if pending is None:
            return self._no_pending(user_id)
        if pending.state != LoginState.AWAITING_CODE:
            return LoginResult(
                LoginOutcome.ERROR,
                pending.state,
                message="There is no login code request to resend.",
            )
        if pending.transport is None:
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.ERROR,
                LoginState.FAILED,
                message="The login attempt is no longer active. Start again with /connect.",
            )

        attempts_left = self._max_code_attempts - pending.code_attempts
        try:
            result = await pending.transport.send_code_request(
                pending.phone, force_sms=force_sms
            )
        except FloodWaitSignal as exc:
            code = _safe_rpc_code(getattr(exc, "code", "") or "FLOOD_WAIT")
            if code in _API_CONFIG_CODES:
                await self._drop(user_id)
                logger.warning("code resend blocked for user %s: %s", user_id, code)
                return _request_failure_result(code, state=LoginState.FAILED)
            return LoginResult(
                LoginOutcome.FLOOD_WAIT,
                pending.state,
                message="Telegram asked us to slow down.",
                retry_after=exc.seconds,
                attempts_left=attempts_left,
            )
        except AccountRevokedError:
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.ERROR,
                LoginState.FAILED,
                message="This login attempt was invalidated. Start again with /connect.",
            )
        except PermanentTransportError as exc:
            code = _safe_rpc_code(exc.code)
            logger.warning("code resend rejected for user %s: %s", user_id, code)
            failure = _request_failure_result(code, state=pending.state)
            if (
                code in _API_CONFIG_CODES
                or code in _PHONE_INVALID
                or code in _PHONE_BANNED
                or code in _PHONE_UNOCCUPIED
                or code.startswith("PHONE_NUMBER_FLOOD")
            ):
                await self._drop(user_id)
                failure.state = LoginState.FAILED
            return failure
        except TransientTransportError:
            self._metrics.incr("login.transient_error")
            return LoginResult(
                LoginOutcome.TRANSIENT_ERROR,
                pending.state,
                message="Temporary network problem. Try resending the code again.",
                attempts_left=attempts_left,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("code resend failed for user %s: %s", user_id, type(exc).__name__)
            return LoginResult(
                LoginOutcome.TRANSIENT_ERROR,
                pending.state,
                message="Could not request another code. Please try again.",
                attempts_left=attempts_left,
            )

        pending.phone_code_hash = result.phone_code_hash
        pending.delivery = result.delivery
        pending.created_at = self._clock()
        self._metrics.incr("login.code_resent")
        logger.info("login code resent for user %s (sms=%s)", user_id, bool(force_sms))
        return LoginResult(
            LoginOutcome.CODE_SENT,
            LoginState.AWAITING_CODE,
            message="Login code requested again.",
            attempts_left=attempts_left,
            delivery=result.delivery,
            next_delivery=result.next_delivery,
        )

    async def submit_code(self, user_id: int, raw_code: str) -> LoginResult:
        pending = self._live_pending(user_id)
        if pending is None:
            return self._no_pending(user_id)
        if pending.state == LoginState.AWAITING_PASSWORD:
            return LoginResult(
                LoginOutcome.PASSWORD_REQUIRED,
                LoginState.AWAITING_PASSWORD,
                message="Two-step verification is enabled. Send your password.",
            )

        try:
            code = normalize_login_code(
                raw_code,
                allow_words=pending.delivery in {"sms_word", "sms_phrase"},
            )
        except InvalidCodeError as exc:
            pending.code_attempts += 1
            attempts_left = self._max_code_attempts - pending.code_attempts
            if attempts_left <= 0:
                return await self._fail_attempts(user_id, "code")
            return LoginResult(
                LoginOutcome.INVALID_CODE,
                LoginState.AWAITING_CODE,
                message=f"That does not look like a login code ({exc}).",
                attempts_left=attempts_left,
            )

        transport = pending.transport
        try:
            account = await transport.sign_in_with_code(pending.phone, code, pending.phone_code_hash)
        except PasswordRequiredSignal:
            pending.state = LoginState.AWAITING_PASSWORD
            self._metrics.incr("login.password_required")
            return LoginResult(
                LoginOutcome.PASSWORD_REQUIRED,
                LoginState.AWAITING_PASSWORD,
                message="Two-step verification is enabled.",
                attempts_left=self._max_password_attempts,
            )
        except FloodWaitSignal as exc:
            return LoginResult(
                LoginOutcome.FLOOD_WAIT,
                pending.state,
                message="Telegram asked us to slow down.",
                retry_after=exc.seconds,
                attempts_left=self._max_code_attempts - pending.code_attempts,
            )
        except AccountRevokedError:
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.ERROR,
                LoginState.FAILED,
                message="This login attempt was invalidated. Please start again.",
            )
        except PermanentTransportError as exc:
            return await self._handle_code_error(user_id, pending, exc)
        except TransientTransportError:
            self._metrics.incr("login.transient_error")
            return LoginResult(
                LoginOutcome.TRANSIENT_ERROR,
                LoginState.AWAITING_CODE,
                message="Temporary network problem. Please send the code again.",
                attempts_left=self._max_code_attempts - pending.code_attempts,
            )
        except Exception as exc:  # pragma: no cover - unexpected
            logger.warning("sign-in failed for user %s: %s", user_id, type(exc).__name__)
            return LoginResult(
                LoginOutcome.ERROR,
                pending.state,
                message="Sign-in failed. Please start again with /connect.",
            )

        return await self._finish(user_id, pending, account)

    async def submit_password(self, user_id: int, password: str) -> LoginResult:
        pending = self._live_pending(user_id)
        if pending is None:
            return self._no_pending(user_id)
        if pending.state != LoginState.AWAITING_PASSWORD:
            return LoginResult(
                LoginOutcome.ERROR,
                pending.state,
                message="No password was requested for this login.",
            )
        if not password:
            return LoginResult(
                LoginOutcome.INVALID_PASSWORD,
                LoginState.AWAITING_PASSWORD,
                message="Password cannot be empty.",
                attempts_left=self._max_password_attempts - pending.password_attempts,
            )

        transport = pending.transport
        try:
            account = await transport.sign_in_with_password(password)
        except FloodWaitSignal as exc:
            return LoginResult(
                LoginOutcome.FLOOD_WAIT,
                LoginState.AWAITING_PASSWORD,
                message="Telegram asked us to slow down.",
                retry_after=exc.seconds,
                attempts_left=self._max_password_attempts - pending.password_attempts,
            )
        except PermanentTransportError as exc:
            if exc.code in _PASSWORD_INVALID:
                pending.password_attempts += 1
                attempts_left = self._max_password_attempts - pending.password_attempts
                if attempts_left <= 0:
                    return await self._fail_attempts(user_id, "password")
                return LoginResult(
                    LoginOutcome.INVALID_PASSWORD,
                    LoginState.AWAITING_PASSWORD,
                    message="That password was not accepted.",
                    attempts_left=attempts_left,
                )
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.ERROR,
                LoginState.FAILED,
                message="Two-step verification failed. Please start again.",
            )
        except TransientTransportError:
            return LoginResult(
                LoginOutcome.TRANSIENT_ERROR,
                LoginState.AWAITING_PASSWORD,
                message="Temporary network problem. Please send the password again.",
                attempts_left=self._max_password_attempts - pending.password_attempts,
            )

        return await self._finish(user_id, pending, account)

    async def abandon(self, user_id: int) -> None:
        await self._drop(user_id)

    async def cleanup_expired(self) -> int:
        """Drop pending logins whose TTL elapsed. Returns how many were dropped."""
        now = self._clock()
        expired = [uid for uid, pending in self._pending.items() if pending.expired(now)]
        for uid in expired:
            await self._drop(uid)
        stale = [
            uid
            for uid, completed in self._completed.items()
            if completed.created_at and now - completed.created_at >= self._ttl
        ]
        for uid in stale:
            self._completed.pop(uid, None)
        return len(expired)

    # -- internals ---------------------------------------------------------
    def _live_pending(self, user_id: int) -> Optional[PendingLogin]:
        pending = self._pending.get(int(user_id))
        if pending is None:
            return None
        if pending.expired(self._clock()):
            return None
        return pending

    def _no_pending(self, user_id: int) -> LoginResult:
        return LoginResult(
            LoginOutcome.NO_PENDING_LOGIN,
            LoginState.IDLE,
            message="There is no login in progress. Use /connect to start.",
        )

    async def _handle_code_error(
        self, user_id: int, pending: PendingLogin, exc: PermanentTransportError
    ) -> LoginResult:
        code = exc.code
        if code in _CODE_INVALID:
            pending.code_attempts += 1
            attempts_left = self._max_code_attempts - pending.code_attempts
            if attempts_left <= 0:
                return await self._fail_attempts(user_id, "code")
            return LoginResult(
                LoginOutcome.INVALID_CODE,
                LoginState.AWAITING_CODE,
                message="That code was not accepted.",
                attempts_left=attempts_left,
            )
        if code in _CODE_EXPIRED:
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.CODE_EXPIRED,
                LoginState.FAILED,
                message="The login code expired. Please start again with /connect.",
            )
        if (
            code in _PHONE_INVALID
            or code in _PHONE_BANNED
            or code in _PHONE_UNOCCUPIED
            or code.startswith("PHONE_NUMBER_FLOOD")
        ):
            await self._drop(user_id)
            logger.warning("phone-related login error for user %s: %s", user_id, code)
            return _request_failure_result(code, state=LoginState.FAILED)
        if code in _API_CONFIG_CODES:
            await self._drop(user_id)
            logger.warning("API configuration login error for user %s: %s", user_id, code)
            return _request_failure_result(code, state=LoginState.FAILED)
        await self._drop(user_id)
        logger.warning("permanent login error for user %s: %s", user_id, code)
        return LoginResult(
            LoginOutcome.ERROR,
            LoginState.FAILED,
            message=f"Telegram rejected the login ({code}).",
        )

    async def _fail_attempts(self, user_id: int, kind: str) -> LoginResult:
        await self._drop(user_id)
        self._metrics.incr(f"login.failed_{kind}")
        return LoginResult(
            LoginOutcome.TOO_MANY_ATTEMPTS,
            LoginState.FAILED,
            message="Too many incorrect attempts. Please start again with /connect.",
        )

    async def _finish(
        self, user_id: int, pending: PendingLogin, account: AccountInfo
    ) -> LoginResult:
        expected = pending.expected_account_id
        if (
            self._require_same_account
            and expected is not None
            and int(account.user_id) != int(expected)
        ):
            logger.warning(
                "account mismatch: user %s expected account %s but authorized %s",
                user_id,
                expected,
                account.user_id,
            )
            await self._drop(user_id)
            return LoginResult(
                LoginOutcome.ACCOUNT_MISMATCH,
                LoginState.FAILED,
                message=(
                    "The Telegram account that authorized is not the account that was "
                    "connected before. For safety this session was discarded."
                ),
            )

        session_string = pending.transport.session_string()
        dc_id = pending.transport.dc_id()
        pending.state = LoginState.AUTHORIZED
        pending.account_user_id = int(account.user_id)
        pending.account_username = account.username
        pending.session_string = session_string
        pending.dc_id = dc_id
        pending.completed = True
        self._completed[int(user_id)] = CompletedLogin(
            user_id=int(user_id),
            session_string=session_string,
            account=account,
            dc_id=dc_id,
            expected_account_id=expected,
            created_at=self._clock(),
        )
        # Drop the credentials-free pending record; the caller persists what it
        # needs through SessionStore (encrypted).
        transport = pending.transport
        self._pending.pop(int(user_id), None)
        if transport is not None:
            await self._safe_disconnect(transport)
        self._metrics.incr("login.authorized")
        logger.info("login authorized for user %s (account=%s)", user_id, account.user_id)
        return LoginResult(
            LoginOutcome.AUTHORIZED,
            LoginState.AUTHORIZED,
            message="Signed in.",
            account_user_id=int(account.user_id),
            account_username=account.username,
        )

    async def _drop(self, user_id: int) -> None:
        self._completed.pop(int(user_id), None)
        pending = self._pending.pop(int(user_id), None)
        if pending and pending.transport is not None:
            await self._safe_disconnect(pending.transport)

    @staticmethod
    async def _safe_disconnect(transport: UserbotTransport) -> None:
        try:
            await transport.disconnect()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("disconnect after login failed: %s", type(exc).__name__)
