"""Connect Account flow: consent -> phone -> login code -> optional 2FA password.

Security guarantees implemented here
------------------------------------
* Everything happens in a **private chat**. Group chats are rejected outright, so
  a login code or a two-step password can never be requested or accepted in a
  public place.
* Phone numbers are entered manually. The flow never requests the user's contact
  card or address book.
* The OTP and the two-step password are handed to
  :class:`~app.sessions.manager.SessionManager`, used inside the coroutine that
  received them and then dropped. They are never written to the FSM, to MongoDB,
  to disk or to the logs. The FSM only ever holds *which step* we are on.
* The user's own message carrying the code/password is deleted (best effort) so
  it does not linger in the chat history.
* The stored session is activated only after Telegram confirms the login. If
  the user is reconnecting a previously stored account, the Telegram account id
  is checked against the known id (``require_same_account``); a first-time login
  cannot be compared with the bot's own user id, which is a different account.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.services import BotServices
from app.bot.states import ConnectFlow
from app.constants import AccountStatus
from app.sessions.states import LoginOutcome, LoginState

logger = logging.getLogger(__name__)

router = Router(name="connect")

# Outcomes that mean "the login attempt is over, go back to the start".
_DEAD_OUTCOMES = frozenset(
    {
        LoginOutcome.CODE_EXPIRED,
        LoginOutcome.TOO_MANY_ATTEMPTS,
        LoginOutcome.ACCOUNT_MISMATCH,
        LoginOutcome.NO_PENDING_LOGIN,
        LoginOutcome.INVALID_PHONE,
        LoginOutcome.ERROR,
    }
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


async def _drop(message: Message) -> None:
    """Best-effort deletion of the user's own sensitive message."""
    try:
        await message.delete()
    except Exception as exc:  # pragma: no cover - depends on chat permissions
        logger.debug("could not delete message: %s", type(exc).__name__)


async def _reset_login(user_id: int, services: BotServices) -> None:
    """Cancel any in-memory login and clear a stale awaiting-code status."""
    await services.sessions.abandon_login(user_id)
    user = await services.repos.users.get(user_id)
    if user is not None and user.status in (
        AccountStatus.AWAITING_CODE.value,
        AccountStatus.AWAITING_PASSWORD.value,
    ):
        await services.repos.users.set_status(user_id, AccountStatus.NEW.value)


async def _ask_for_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(ConnectFlow.phone)
    # Remove any old contact-share keyboard cached by clients from an earlier
    # deployment. The flow below accepts typed numbers only.
    await message.answer(texts.connect_phone_prompt(), reply_markup=ReplyKeyboardRemove())


async def _ask_for_code(message: Message, state: FSMContext, *, note: str = "") -> None:
    await state.set_state(ConnectFlow.code)
    body = texts.connect_code_prompt()
    if note:
        body = f"{note}\n\n{body}"
    await message.answer(body, reply_markup=kb.code_keyboard())


async def _edit_code_prompt(callback: CallbackQuery, body: str) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(body, reply_markup=kb.code_keyboard())
    except Exception as exc:  # pragma: no cover - Telegram may reject identical edits
        logger.debug("could not edit code prompt: %s", type(exc).__name__)


async def _ask_for_password(message: Message, state: FSMContext, *, note: str = "") -> None:
    await state.set_state(ConnectFlow.password)
    body = texts.connect_password_prompt()
    if note:
        body = f"{note}\n\n{body}"
    await message.answer(body, reply_markup=kb.cancel_keyboard())


async def _abort_flow(message: Message, state: FSMContext, body: str) -> None:
    await state.clear()
    await message.answer(body, reply_markup=ReplyKeyboardRemove())


async def _activate(
    message: Message,
    state: FSMContext,
    services: BotServices,
    *,
    user_id: int,
) -> None:
    """Persist the finished login, then start the worker for this account."""
    try:
        completed = await services.sessions.finalize_login(user_id)
    except Exception as exc:  # pragma: no cover - storage/database failure
        logger.exception("could not activate login for %s: %s", user_id, type(exc).__name__)
        await state.clear()
        await message.answer(
            "⚠️ The login was authorized but its encrypted session could not be saved. "
            "Please try /connect again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await state.clear()
    if completed is None:
        await message.answer(
            "⚠️ The login could not be activated. Please run /start and try again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    account = completed.account
    fingerprint = None
    user = await services.repos.users.get(user_id)
    if user is not None:
        fingerprint = (user.account or {}).get("fingerprint")

    worker = await services.userbots.ensure(user_id)
    if worker is not None:
        await message.answer(
            texts.connected_text(
                {
                    "user_id": int(account.user_id),
                    "username": account.username,
                    "first_name": account.first_name,
                },
                fingerprint=fingerprint,
            ),
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        # Honest reporting: the session is stored, but no worker slot was free.
        await message.answer(
            "✅ Your encrypted session is saved, but no userbot worker slot was available.\n\n"
            "Open the dashboard and press <b>Status</b> to try again. If the service is at "
            "capacity, ask the operator to raise <code>MAX_ACTIVE_USERBOTS</code>. Your session "
            "is not lost.",
            reply_markup=ReplyKeyboardRemove(),
        )
    await message.answer(
        "🎛 Your dashboard:", reply_markup=kb.main_menu(user, is_admin=services.is_admin(user_id))
    )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "act:connect")
async def cb_connect(callback: CallbackQuery, state: FSMContext, services: BotServices) -> None:
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Connect your account in a private chat with the bot.", show_alert=True)
        return

    user_id = int(callback.from_user.id)
    user = await services.register_user(callback.from_user)
    if user.is_banned:
        await callback.answer("🚫 This account is suspended. Contact the operator.", show_alert=True)
        return

    if not user.consent_given:
        await callback.message.edit_text(texts.SECURITY_NOTICE, reply_markup=kb.consent_menu())
        await callback.answer()
        return

    await _reset_login(user_id, services)
    await _ask_for_phone(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "act:consent")
async def cb_consent(callback: CallbackQuery, state: FSMContext, services: BotServices) -> None:
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Please consent in a private chat with the bot.", show_alert=True)
        return
    user_id = int(callback.from_user.id)
    await services.register_user(callback.from_user)
    await services.repos.users.set_consent(user_id, True)
    await services.repos.audit.record(user_id, "session.consent_given", target_user_id=user_id)
    await callback.message.edit_text("✅ Consent recorded.")
    await _reset_login(user_id, services)
    await _ask_for_phone(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "act:cancel_login")
async def cb_cancel_login(callback: CallbackQuery, state: FSMContext, services: BotServices) -> None:
    user_id = int(callback.from_user.id)
    await services.sessions.abandon_login(user_id)
    current = await services.repos.users.get(user_id)
    if current is not None and current.status in (
        AccountStatus.AWAITING_CODE.value,
        AccountStatus.AWAITING_PASSWORD.value,
    ):
        await services.repos.users.set_status(user_id, AccountStatus.NEW.value)
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(
            "✖️ Login cancelled. Nothing was stored.",
            reply_markup=kb.main_menu(current, is_admin=services.is_admin(user_id)),
        )
    await callback.answer("Cancelled.")


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext, services: BotServices) -> None:
    if not _is_private(message):
        await message.answer("🔒 Account login only works in a private chat.")
        return

    user = await services.register_user(message.from_user)
    if user.is_banned:
        await message.answer("🚫 This account is suspended. Contact the operator.")
        return
    if not user.consent_given:
        await state.clear()
        await message.answer(texts.SECURITY_NOTICE, reply_markup=kb.consent_menu())
        return

    await _reset_login(int(message.from_user.id), services)
    await _ask_for_phone(message, state)


@router.callback_query(F.data.in_({"act:resend_code", "act:resend_sms"}))
async def cb_resend_code(
    callback: CallbackQuery, state: FSMContext, services: BotServices
) -> None:
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Resend the code in a private chat with the bot.", show_alert=True)
        return

    user_id = int(callback.from_user.id)
    force_sms = callback.data == "act:resend_sms"
    result = await services.sessions.resend_code(user_id, force_sms=force_sms)

    if result.outcome is LoginOutcome.CODE_SENT:
        await state.set_state(ConnectFlow.code)
        note = texts.code_delivery_text(result.delivery)
        body = f"{note}\n\n{texts.connect_code_prompt()}"
        await _edit_code_prompt(callback, body)
        await callback.answer("New code requested.")
        return

    if result.outcome is LoginOutcome.FLOOD_WAIT:
        wait = max(1, int(result.retry_after) or 60)
        await callback.answer(
            f"⏳ Telegram says wait about {wait}s before requesting another code.",
            show_alert=True,
        )
        return

    if result.outcome is LoginOutcome.TRANSIENT_ERROR:
        await callback.answer(
            _esc(result.message) or "Temporary network problem. Try again.",
            show_alert=True,
        )
        return

    if result.outcome is LoginOutcome.ERROR and result.state is LoginState.AWAITING_CODE:
        # The login is still usable (for example, Telegram refused SMS only).
        note = f"⚠️ {_esc(result.message) or 'The code could not be resent.'}"
        await _edit_code_prompt(callback, f"{note}\n\n{texts.connect_code_prompt()}")
        await callback.answer("Code not resent.", show_alert=True)
        return

    if result.outcome is LoginOutcome.ERROR and result.state is LoginState.AWAITING_PASSWORD:
        await callback.answer(
            "Enter your two-step password to continue; resend is no longer available.",
            show_alert=True,
        )
        return

    await state.clear()
    user = await services.repos.users.get(user_id)
    body = (
        f"⚠️ {_esc(result.message) or 'The login could not be resumed.'}\n\n"
        "Send /start or /connect to try again."
    )
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                body, reply_markup=kb.main_menu(user, is_admin=services.is_admin(user_id))
            )
        except Exception as exc:  # pragma: no cover - Telegram may reject the edit
            logger.debug("could not show resend failure: %s", type(exc).__name__)
    await callback.answer("Login ended.", show_alert=True)


# ---------------------------------------------------------------------------
# step 1: phone number
# ---------------------------------------------------------------------------
@router.message(StateFilter(ConnectFlow.phone), F.text)
async def on_phone(message: Message, state: FSMContext, services: BotServices) -> None:
    if not _is_private(message):
        await _abort_flow(message, state, "🔒 Account login only works in a private chat.")
        return
    await _submit_phone(message, state, services, message.text or "")


async def _submit_phone(
    message: Message, state: FSMContext, services: BotServices, raw_phone: str
) -> None:
    user_id = int(message.from_user.id)

    # Deferred import: keeps the module import graph shallow.
    from app.sessions.states import InvalidPhoneError, normalize_phone

    try:
        phone = normalize_phone(raw_phone)
    except InvalidPhoneError as exc:
        await message.answer(
            f"⚠️ {_esc(exc)}\n\nTry again, for example <code>+919876543210</code>."
        )
        return

    status = await message.answer("📡 Requesting a login code from Telegram…")
    try:
        result = await services.sessions.start_login(user_id, phone)
    except Exception as exc:  # pragma: no cover - DB/transport failure
        logger.warning("login start failed for %s: %s", user_id, type(exc).__name__)
        await status.edit_text(
            "⚠️ Could not start the login right now. Please try /start again."
        )
        await state.clear()
        return

    if result.outcome is LoginOutcome.CODE_SENT:
        # Never echo the phone number back: only a masked reminder.
        masked = phone[-4:] if len(phone) > 5 else "…"
        await status.edit_text(f"✉️ Login code requested for •••{masked}.")
        await _ask_for_code(
            message,
            state,
            note=texts.code_delivery_text(result.delivery),
        )
        return

    if result.outcome is LoginOutcome.FLOOD_WAIT:
        await status.edit_text(
            f"⏳ Telegram is rate limiting code requests. Please wait about "
            f"{int(result.retry_after) or 60}s and try again with /start."
        )
        await state.clear()
        return

    await status.edit_text(f"⚠️ {_esc(result.message) or 'Could not request a login code.'}")
    if result.outcome in _DEAD_OUTCOMES:
        await state.clear()


# ---------------------------------------------------------------------------
# step 2: login code
# ---------------------------------------------------------------------------
@router.message(StateFilter(ConnectFlow.code), F.text)
async def on_code(message: Message, state: FSMContext, services: BotServices) -> None:
    if not _is_private(message):
        await _abort_flow(message, state, "🔒 Account login only works in a private chat.")
        return

    user_id = int(message.from_user.id)
    raw_code = message.text or ""
    # Remove the message that carries the OTP before doing anything else.
    await _drop(message)

    result = await services.sessions.submit_code(user_id, raw_code)
    await _handle_login_result(message, state, services, result, kind="code")


# ---------------------------------------------------------------------------
# step 3: two-step verification password
# ---------------------------------------------------------------------------
@router.message(StateFilter(ConnectFlow.password), F.text)
async def on_password(message: Message, state: FSMContext, services: BotServices) -> None:
    if not _is_private(message):
        await _abort_flow(message, state, "🔒 Account login only works in a private chat.")
        return

    user_id = int(message.from_user.id)
    password = message.text or ""
    await _drop(message)

    result = await services.sessions.submit_password(user_id, password)
    await _handle_login_result(message, state, services, result, kind="password")


async def _handle_login_result(
    message: Message,
    state: FSMContext,
    services: BotServices,
    result,
    *,
    kind: str,
) -> None:
    """Translate a LoginResult into the next bot message / state."""
    user_id = int(message.from_user.id)
    outcome = result.outcome

    if outcome is LoginOutcome.AUTHORIZED:
        await _activate(message, state, services, user_id=user_id)
        return

    if outcome is LoginOutcome.PASSWORD_REQUIRED:
        await _ask_for_password(
            message, state, note="🔑 Two-step verification is enabled."
        )
        return

    if outcome is LoginOutcome.INVALID_CODE:
        left = result.attempts_left
        note = _esc(result.message) or "That code was not accepted."
        if left:
            note = f"{note} ({left} attempt(s) left.)"
        await _ask_for_code(message, state, note=f"⚠️ {note}")
        return

    if outcome is LoginOutcome.INVALID_PASSWORD:
        left = result.attempts_left
        note = _esc(result.message) or "That password was not accepted."
        if left:
            note = f"{note} ({left} attempt(s) left.)"
        await _ask_for_password(message, state, note=f"⚠️ {note}")
        return

    if outcome is LoginOutcome.FLOOD_WAIT:
        wait = int(result.retry_after) or 60
        body = (
            f"⏳ Telegram asked us to slow down. Please send the login code again in "
            f"about {wait}s."
        )
        if kind == "password":
            await _ask_for_password(message, state, note=body)
        else:
            await _ask_for_code(message, state, note=body)
        return

    if outcome is LoginOutcome.TRANSIENT_ERROR:
        body = f"🌐 {_esc(result.message) or 'Temporary network problem.'}"
        if kind == "password":
            await _ask_for_password(message, state, note=body)
        else:
            await _ask_for_code(message, state, note=body)
        return

    # Everything else ends the attempt.
    await _abort_flow(
        message,
        state,
        f"⚠️ {_esc(result.message) or 'The login could not be completed.'}\n\n"
        "Send /start to try again.",
    )


__all__ = ["router"]
