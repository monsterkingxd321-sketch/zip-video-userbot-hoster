from __future__ import annotations

import pytest

from app.sessions.login import LoginManager
from app.sessions.states import (
    InvalidCodeError,
    LoginOutcome,
    LoginState,
    normalize_login_code,
)
from app.userbot.transport import (
    AccountInfo,
    CodeRequestResult,
    PasswordRequiredSignal,
    PermanentTransportError,
)
from tests.fakes import FakeTransport


class LoginTransport(FakeTransport):
    def __init__(self, *, account_id=555, password_required=False):
        super().__init__(user_id=account_id)
        self.account = AccountInfo(
            user_id=account_id,
            username="login_user",
            first_name="Login",
            phone="+15550001111",
        )
        self.password_required = password_required
        self.code_attempts = []
        self.code_requests = []

    async def send_code_request(self, phone, *, force_sms=False):
        self.code_requests.append((phone, force_sms))
        return CodeRequestResult(
            phone_code_hash=f"hash-{len(self.code_requests)}",
            delivery="sms" if force_sms else "app",
            next_delivery="sms",
        )

    async def sign_in_with_code(self, phone, code, phone_code_hash):
        self.code_attempts.append(code)
        if self.password_required:
            raise PasswordRequiredSignal("password needed")
        if code != "12345":
            raise PermanentTransportError("PHONE_CODE_INVALID", "invalid code")
        return self.account

    async def sign_in_with_password(self, password):
        if password != "correct-password":
            raise PermanentTransportError("PASSWORD_HASH_INVALID", "invalid password")
        return self.account


def make_manager(
    transports,
    *,
    expected=None,
    require_same_account=True,
    clock=None,
    transport_cls=LoginTransport,
):
    created = []

    def factory(user_id: int, session: str):
        transport = transport_cls()
        transports.append(transport)
        created.append(transport)
        return transport

    kwargs = dict(
        transport_factory=factory,
        ttl_seconds=60,
        max_code_attempts=3,
        max_password_attempts=3,
        require_same_account=require_same_account,
    )
    if clock is not None:
        kwargs["clock"] = clock
    return LoginManager(**kwargs)


@pytest.mark.asyncio
async def test_login_code_flow_and_transient_retry():
    transports = []
    manager = make_manager(transports)
    first = await manager.begin(1, "+1 (555) 000-1111")
    assert first.outcome is LoginOutcome.CODE_SENT
    assert first.state is LoginState.AWAITING_CODE

    invalid = await manager.submit_code(1, "99999")
    assert invalid.outcome is LoginOutcome.INVALID_CODE
    assert invalid.attempts_left == 2

    authorized = await manager.submit_code(1, "12345")
    assert authorized.outcome is LoginOutcome.AUTHORIZED
    completed = manager.take_completed(1)
    assert completed is not None
    assert completed.session_string.startswith("1")
    assert manager.has_pending(1) is False


@pytest.mark.asyncio
async def test_password_flow():
    transports = []

    def factory(user_id, session):
        transport = LoginTransport(password_required=True)
        transports.append(transport)
        return transport

    manager = LoginManager(
        transport_factory=factory,
        require_same_account=True,
        max_password_attempts=2,
    )
    await manager.begin(2, "+15550002222")
    required = await manager.submit_code(2, "12345")
    assert required.outcome is LoginOutcome.PASSWORD_REQUIRED
    assert required.state is LoginState.AWAITING_PASSWORD

    bad = await manager.submit_password(2, "wrong")
    assert bad.outcome is LoginOutcome.INVALID_PASSWORD
    assert bad.attempts_left == 1

    good = await manager.submit_password(2, "correct-password")
    assert good.outcome is LoginOutcome.AUTHORIZED


@pytest.mark.asyncio
async def test_reconnect_account_mismatch_discards_session():
    transports = []
    manager = make_manager(transports)
    await manager.begin(3, "+15550003333", expected_account_id=999)
    result = await manager.submit_code(3, "12345")
    assert result.outcome is LoginOutcome.ACCOUNT_MISMATCH
    assert manager.take_completed(3) is None
    assert transports[0].connected is False


@pytest.mark.asyncio
async def test_first_login_is_not_compared_to_bot_user_id():
    transports = []
    manager = make_manager(transports, require_same_account=True)
    await manager.begin(4, "+15550004444")  # no expected account id
    result = await manager.submit_code(4, "12345")
    assert result.outcome is LoginOutcome.AUTHORIZED
    assert manager.take_completed(4) is not None


@pytest.mark.asyncio
async def test_completed_and_pending_logins_expire():
    now = [100.0]
    transports = []
    manager = make_manager(transports, clock=lambda: now[0])
    await manager.begin(5, "+15550005555")
    now[0] += 61
    assert manager.has_pending(5) is False
    assert manager.state_of(5) is LoginState.EXPIRED

    await manager.begin(6, "+15550006666")
    assert (await manager.submit_code(6, "12345")).outcome is LoginOutcome.AUTHORIZED
    assert manager.take_completed(6) is not None

    # Put a new completed record in place and verify the TTL drops it.
    await manager.begin(7, "+15550007777")
    await manager.submit_code(7, "12345")
    now[0] += 61
    await manager.cleanup_expired()
    assert manager.take_completed(7) is None


@pytest.mark.asyncio
async def test_abandon_disconnects_and_clears_completed():
    transports = []
    manager = make_manager(transports)
    await manager.begin(8, "+15550008888")
    await manager.abandon(8)
    assert manager.has_pending(8) is False
    assert transports[0].connected is False


@pytest.mark.asyncio
async def test_resend_code_reports_delivery_and_can_force_sms():
    transports = []
    manager = make_manager(transports)
    first = await manager.begin(20, "+15550002020")
    assert first.outcome is LoginOutcome.CODE_SENT
    assert first.delivery == "app"

    resent = await manager.resend_code(20)
    assert resent.outcome is LoginOutcome.CODE_SENT
    assert resent.delivery == "app"
    assert transports[0].code_requests == [("+15550002020", False), ("+15550002020", False)]

    sms = await manager.resend_code(20, force_sms=True)
    assert sms.outcome is LoginOutcome.CODE_SENT
    assert sms.delivery == "sms"
    assert transports[0].code_requests[-1] == ("+15550002020", True)

    # The updated phone_code_hash must be the one used by the next sign-in.
    authorized = await manager.submit_code(20, "12345")
    assert authorized.outcome is LoginOutcome.AUTHORIZED


@pytest.mark.asyncio
async def test_resend_without_pending_login_is_reported():
    manager = make_manager([])
    result = await manager.resend_code(21)
    assert result.outcome is LoginOutcome.NO_PENDING_LOGIN


@pytest.mark.asyncio
async def test_api_id_error_is_not_reported_as_invalid_phone():
    class ApiFailTransport(FakeTransport):
        async def send_code_request(self, phone, *, force_sms=False):
            raise PermanentTransportError("API_ID_INVALID", "request failed")

    manager = LoginManager(transport_factory=lambda user_id, session: ApiFailTransport())
    result = await manager.begin(22, "+15550002222")
    assert result.outcome is LoginOutcome.ERROR
    assert "API_ID_INVALID" in result.message
    assert "phone number" not in result.message.lower()


def test_word_and_phrase_codes_are_normalized_only_when_allowed():
    assert normalize_login_code(" 1 2 3 4 5 ") == "12345"
    with pytest.raises(InvalidCodeError):
        normalize_login_code("secret word")
    assert normalize_login_code("  secret   word  ", allow_words=True) == "secret word"
    assert normalize_login_code("  hello   world ", allow_words=True) == "hello world"


@pytest.mark.asyncio
async def test_phrase_code_delivery_accepts_word_code():
    class PhraseTransport(LoginTransport):
        async def send_code_request(self, phone, *, force_sms=False):
            self.code_requests.append((phone, force_sms))
            return CodeRequestResult(
                phone_code_hash="phrase-hash",
                delivery="sms_phrase",
                next_delivery="sms",
            )

        async def sign_in_with_code(self, phone, code, phone_code_hash):
            self.code_attempts.append(code)
            return self.account

    transports = []
    manager = make_manager(transports, transport_cls=PhraseTransport)
    first = await manager.begin(23, "+15550002323")
    assert first.delivery == "sms_phrase"

    authorized = await manager.submit_code(23, "  hello   world ")
    assert authorized.outcome is LoginOutcome.AUTHORIZED
    assert transports[0].code_attempts == ["hello world"]
