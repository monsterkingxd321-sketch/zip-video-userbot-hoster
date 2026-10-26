"""User dashboard handlers: /start, status, jobs, settings, disconnect."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.services import BotServices
from app.constants import ACTIVE_TASK_STATUSES, AccountStatus
from app.sessions.store import OwnershipError

logger = logging.getLogger(__name__)

router = Router(name="dashboard")


def _is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def _reject_group(message: Message, services: BotServices) -> bool:
    """Never handle account operations in a group chat."""
    if _is_private(message):
        return False
    await message.reply(
        "🔒 For your safety, account controls only work in a <b>private chat</b> with me. "
        "Open a direct chat and send /start."
    )
    return True


async def _reject_group_callback(callback: CallbackQuery) -> bool:
    chat = callback.message.chat if callback.message else None
    if chat is not None and chat.type == ChatType.PRIVATE:
        return False
    await callback.answer(
        "Account controls only work in a private chat with the bot.", show_alert=True
    )
    return True


async def _render_dashboard(services: BotServices, user_id: int, *, edit: Message | None = None) -> None:
    user = await services.repos.users.get(user_id)
    text = texts.dashboard_text(user, is_admin=services.is_admin(user_id)) if user else texts.WELCOME
    markup = kb.main_menu(user, is_admin=services.is_admin(user_id))
    if edit is not None:
        try:
            await edit.edit_text(text, reply_markup=markup)
            return
        except Exception:  # pragma: no cover - message may be identical
            pass
    if services.bot is None:
        return
    await services.bot.send_message(user_id, text, reply_markup=markup)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, services: BotServices) -> None:
    if await _reject_group(message, services):
        return
    await state.clear()
    await services.register_user(message.from_user)
    user = await services.repos.users.get(message.from_user.id)
    await message.answer(texts.WELCOME)
    await message.answer(
        texts.dashboard_text(user, is_admin=services.is_admin(message.from_user.id)),
        reply_markup=kb.main_menu(user, is_admin=services.is_admin(message.from_user.id)),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, services: BotServices) -> None:
    if await _reject_group(message, services):
        return
    await message.answer(texts.WELCOME)


@router.message(Command("status"))
async def cmd_status(message: Message, services: BotServices) -> None:
    if await _reject_group(message, services):
        return
    await services.register_user(message.from_user)
    user = await services.repos.users.get(message.from_user.id)
    if (
        services.userbots.worker_for(message.from_user.id) is None
        and user is not None
        and user.status == AccountStatus.CONNECTED.value
    ):
        await services.userbots.ensure(message.from_user.id)
        user = await services.repos.users.get(message.from_user.id)
    await message.answer(
        texts.dashboard_text(user), reply_markup=kb.main_menu(user, is_admin=services.is_admin(message.from_user.id))
    )


@router.message(Command("jobs"))
async def cmd_jobs(message: Message, services: BotServices) -> None:
    if await _reject_group(message, services):
        return
    tasks = await services.repos.tasks.list_for_user(message.from_user.id, limit=10)
    await message.answer(texts.tasks_text(tasks), reply_markup=kb.tasks_menu(tasks))


@router.message(Command("settings"))
async def cmd_settings(message: Message, services: BotServices) -> None:
    if await _reject_group(message, services):
        return
    await services.register_user(message.from_user)
    user = await services.repos.users.get(message.from_user.id)
    await message.answer(
        "⚙️ <b>Settings</b>\n\nDefaults applied to new jobs.",
        reply_markup=kb.settings_menu(user),
    )


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message, command: CommandObject, services: BotServices
) -> None:
    if await _reject_group(message, services):
        return
    scope = (command.args or "").strip().lower()
    if scope in ("all", "everything"):
        count = await services.userbots.cancel_all_for_user(message.from_user.id)
        await message.answer(f"🚫 Cancelling {count} job(s).")
        return
    tasks = await services.repos.tasks.list_for_user(
        message.from_user.id, statuses=list(ACTIVE_TASK_STATUSES), limit=1
    )
    if not tasks:
        await message.answer("There is nothing to cancel.")
        return
    await services.repos.tasks.request_cancel(tasks[0].task_id)
    await services.userbots.cancel(message.from_user.id, tasks[0].task_id)
    await message.answer(f"🚫 Cancelling job <code>{tasks[0].task_id[:8]}</code>.")


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "nav:home")
async def cb_home(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await services.register_user(callback.from_user)
    user = await services.repos.users.get(callback.from_user.id)
    await callback.message.edit_text(
        texts.dashboard_text(user, is_admin=services.is_admin(callback.from_user.id)),
        reply_markup=kb.main_menu(user, is_admin=services.is_admin(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data == "nav:status")
async def cb_status(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    user = await services.repos.users.get(callback.from_user.id)
    worker = services.userbots.worker_for(callback.from_user.id)
    if worker is None and user is not None and user.status == AccountStatus.CONNECTED.value:
        # Bring an on-demand worker back (for example after a capacity eviction
        # or a restart where only some accounts were restored).
        worker = await services.userbots.ensure(callback.from_user.id)
    lines = [texts.dashboard_text(user, is_admin=services.is_admin(callback.from_user.id))]
    if worker is not None:
        snapshot = worker.snapshot()
        lines += [
            "",
            "🤖 <b>Live worker</b>",
            f"Queued: {snapshot.queued}",
            f"Running: {len(snapshot.active)}",
            f"Completed: {snapshot.jobs_done} · Failed: {snapshot.jobs_failed}",
        ]
    lines += [
        "",
        "<i>The userbot acts from your Telegram account. It is online while a worker "
        "is resident for your account.</i>",
    ]
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "nav:tasks")
async def cb_tasks(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    tasks = await services.repos.tasks.list_for_user(callback.from_user.id, limit=10)
    await callback.message.edit_text(texts.tasks_text(tasks), reply_markup=kb.tasks_menu(tasks))
    await callback.answer()


@router.callback_query(F.data == "nav:settings")
async def cb_settings(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await services.register_user(callback.from_user)
    user = await services.repos.users.get(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ <b>Settings</b>\n\nDefaults applied to new jobs.", reply_markup=kb.settings_menu(user)
    )
    await callback.answer()


@router.callback_query(F.data == "nav:spoiler")
async def cb_spoiler(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await callback.message.edit_text(texts.SPOILER_HELP, reply_markup=kb.back_home_menu())
    await callback.answer()


@router.callback_query(F.data == "nav:security")
async def cb_security(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await callback.message.edit_text(
        texts.SECURITY_NOTICE, reply_markup=kb.security_menu()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("job:"))
async def cb_job(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    task_id = callback.data.split(":", 1)[1]
    task = await services.repos.tasks.get(task_id)
    if task is None or int(task.user_id) != int(callback.from_user.id):
        await callback.answer("That job does not belong to you.", show_alert=True)
        return
    await callback.message.edit_text(
        texts.task_detail_text(task), reply_markup=kb.task_menu(task)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("jcancel:"))
async def cb_job_cancel(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    task_id = callback.data.split(":", 1)[1]
    task = await services.repos.tasks.get(task_id)
    if task is None or int(task.user_id) != int(callback.from_user.id):
        await callback.answer("That job does not belong to you.", show_alert=True)
        return
    await services.repos.tasks.request_cancel(task_id)
    await services.userbots.cancel(callback.from_user.id, task_id)
    await callback.answer("Cancellation requested.")


# ---------------------------------------------------------------------------
# settings toggles
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "set:spoiler")
async def cb_toggle_spoiler(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await services.register_user(callback.from_user)
    user = await services.repos.users.get(callback.from_user.id)
    current = bool((user.settings or {}).get("spoiler_default"))
    await services.repos.users.set_settings(callback.from_user.id, spoiler_default=not current)
    user = await services.repos.users.get(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ <b>Settings</b>\n\nDefaults applied to new jobs.", reply_markup=kb.settings_menu(user)
    )
    await callback.answer("Spoiler default updated.")


@router.callback_query(F.data == "set:notify")
async def cb_toggle_notify(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    await services.register_user(callback.from_user)
    user = await services.repos.users.get(callback.from_user.id)
    current = bool((user.settings or {}).get("notify_on_complete", True))
    await services.repos.users.set_settings(callback.from_user.id, notify_on_complete=not current)
    user = await services.repos.users.get(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ <b>Settings</b>\n\nDefaults applied to new jobs.", reply_markup=kb.settings_menu(user)
    )
    await callback.answer("Setting updated.")


@router.callback_query(F.data == "act:cancel_all")
async def cb_cancel_all(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    count = await services.userbots.cancel_all_for_user(callback.from_user.id)
    await callback.answer(f"Cancellation requested for {count} job(s).", show_alert=True)


# ---------------------------------------------------------------------------
# disconnect / delete
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "act:disconnect")
async def cb_disconnect(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    user_id = int(callback.from_user.id)
    await services.userbots.stop_user(user_id)
    await services.repos.users.set_status(user_id, AccountStatus.DISCONNECTED.value)
    await services.repos.audit.record(user_id, "session.disconnected", target_user_id=user_id)
    logger.info("user %s disconnected their userbot", user_id)
    user = await services.repos.users.get(user_id)
    await callback.message.edit_text(
        "🔌 <b>Disconnected.</b>\n\nYour encrypted session is still stored, so you can "
        "reconnect with one tap. Use <b>Delete Session</b> to destroy it permanently.",
        reply_markup=kb.main_menu(user, is_admin=services.is_admin(user_id)),
    )
    await callback.answer("Disconnected.")


@router.callback_query(F.data == "act:delete")
async def cb_delete(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ Yes, delete permanently", callback_data="act:delete_confirm")
    builder.button(text="⬅️ Back", callback_data="nav:home")
    builder.adjust(1)
    await callback.message.edit_text(
        "🗑 <b>Delete session?</b>\n\n"
        "This removes the encrypted session from the service. The account stops working "
        "immediately. You can also end it yourself in Telegram → Settings → Devices.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "act:delete_confirm")
async def cb_delete_confirm(callback: CallbackQuery, services: BotServices) -> None:
    if await _reject_group_callback(callback):
        return
    user_id = int(callback.from_user.id)
    await services.userbots.stop_user(user_id)
    try:
        await services.sessions.delete_session(user_id, requester_id=user_id)
    except OwnershipError:  # pragma: no cover - defence in depth
        await callback.answer("Not allowed.", show_alert=True)
        return
    user = await services.repos.users.get(user_id)
    await callback.message.edit_text(
        "🗑 <b>Session deleted.</b>\n\nNothing of your session remains on the service.",
        reply_markup=kb.main_menu(user, is_admin=services.is_admin(user_id)),
    )
    await callback.answer("Deleted.")


__all__ = ["router"]
