"""Login state machine primitives (no network access, fully unit testable)."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.constants import AccountStatus

_WHITESPACE = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"[^\d]")


class InvalidPhoneError(ValueError):
    pass


class InvalidCodeError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Normalize a phone number into ``+<digits>`` international form.

    Accepts spaces, dashes, parentheses, dots and a leading ``00``.
    """
    if not raw or not str(raw).strip():
        raise InvalidPhoneError("Please send your phone number in international format.")
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    # Reject letters and other junk instead of silently turning "abc123" into
    # a valid-looking phone number.
    if not re.fullmatch(r"[+\d\s().\-]+", text):
        raise InvalidPhoneError("Phone number contains characters that are not allowed.")
    if "+" in text[1:]:
        raise InvalidPhoneError("The leading '+' is only allowed at the start.")
    # Keep an explicit '+' only at the start; drop every other separator.
    plus = text.startswith("+")
    stripped = _NON_DIGIT.sub("", text)
    if not plus and stripped.startswith("00"):
        stripped = stripped[2:]
    if not stripped.isdigit():
        raise InvalidPhoneError("Phone number must contain digits only.")
    if not (7 <= len(stripped) <= 15):
        raise InvalidPhoneError("Phone number must be 7-15 digits including the country code.")
    return f"+{stripped}"


def normalize_login_code(raw: str, *, allow_words: bool = False) -> str:
    """Normalize a login code.

    Telegram codes are usually digits shown as e.g. ``1 2 3 4 5``, so *all*
    unicode whitespace and common separators are removed before validation.

    Telegram can also deliver a *secret word* or *secret phrase* by SMS
    (``SentCodeTypeSmsWord`` / ``SentCodeTypeSmsPhrase``). In that case the
    spaces inside the phrase are significant, so they are collapsed to one
    space instead of being removed.
    """
    if raw is None:
        raise InvalidCodeError("empty code")
    text = unicodedata.normalize("NFKC", str(raw))
    text = text.replace("\u200b", "").replace("\ufeff", "")

    if allow_words:
        text = _WHITESPACE.sub(" ", text).strip()
        if not text:
            raise InvalidCodeError("empty code")
        # Secret words/phrases are human-readable words. Keep apostrophes and
        # hyphens, reject HTML/control characters, and cap the length.
        if not re.fullmatch(r"[\w'’\- ]{1,100}", text, re.UNICODE):
            raise InvalidCodeError("login word or phrase contains unsupported characters")
        return text

    text = _WHITESPACE.sub("", text)
    text = text.replace("-", "").replace(".", "").replace("_", "")
    text = text.strip()
    if not text:
        raise InvalidCodeError("empty code")
    if not text.isdigit():
        raise InvalidCodeError("login code must contain digits only")
    if not (3 <= len(text) <= 10):
        raise InvalidCodeError("login code has an unexpected length")
    return text


class LoginState(str, Enum):
    IDLE = "idle"
    AWAITING_CODE = "awaiting_code"
    AWAITING_PASSWORD = "awaiting_password"
    AUTHORIZED = "authorized"
    FAILED = "failed"
    EXPIRED = "expired"


class LoginOutcome(str, Enum):
    CODE_SENT = "code_sent"
    PASSWORD_REQUIRED = "password_required"
    AUTHORIZED = "authorized"
    INVALID_PHONE = "invalid_phone"
    INVALID_CODE = "invalid_code"
    CODE_EXPIRED = "code_expired"
    INVALID_PASSWORD = "invalid_password"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    ACCOUNT_MISMATCH = "account_mismatch"
    NO_PENDING_LOGIN = "no_pending_login"
    EXPIRED = "expired"
    FLOOD_WAIT = "flood_wait"
    TRANSIENT_ERROR = "transient_error"
    ALREADY_AUTHORIZED = "already_authorized"
    ERROR = "error"


@dataclass
class LoginResult:
    outcome: LoginOutcome
    state: LoginState
    message: str = ""
    retry_after: float = 0.0
    attempts_left: int = 0
    delivery: Optional[str] = None
    next_delivery: Optional[str] = None
    account_user_id: Optional[int] = None
    account_username: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            LoginOutcome.CODE_SENT,
            LoginOutcome.PASSWORD_REQUIRED,
            LoginOutcome.AUTHORIZED,
        )


@dataclass
class PendingLogin:
    """In-memory only: never persisted, never logged.

    Deliberately holds no OTP or password: those are used and discarded within
    the coroutine that received them.
    """

    user_id: int
    phone: str
    phone_code_hash: str = ""
    state: LoginState = LoginState.AWAITING_CODE
    delivery: Optional[str] = None
    transport: object = None
    created_at: float = field(default_factory=time.monotonic)
    ttl_seconds: float = 900.0
    code_attempts: int = 0
    password_attempts: int = 0
    expected_account_id: Optional[int] = None
    account_user_id: Optional[int] = None
    account_username: Optional[str] = None
    session_string: Optional[str] = None
    dc_id: Optional[int] = None
    completed: bool = False

    def expired(self, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        return (current - self.created_at) > self.ttl_seconds

    def ages_seconds(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else now
        return current - self.created_at


def state_for_account_status(status: str) -> LoginState:
    if status == AccountStatus.AWAITING_CODE.value:
        return LoginState.AWAITING_CODE
    if status == AccountStatus.AWAITING_PASSWORD.value:
        return LoginState.AWAITING_PASSWORD
    if status == AccountStatus.CONNECTED.value:
        return LoginState.AUTHORIZED
    return LoginState.IDLE
