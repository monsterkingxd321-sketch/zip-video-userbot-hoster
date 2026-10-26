"""Operator dashboard: /admin plus per-user inspection and controls.

Rules enforced here
-------------------
* Every entry point checks :meth:`Settings.is_admin` first; a non-admin gets a
  short refusal and nothing else happens.
* Nothing secret is ever rendered from the database. The collections we read
  (``users``, ``tasks``, ``sessions``) only ever store masked phone numbers, a
  session *fingerprint* and ciphertext. Session strings, OTPs and two-step
  passwords are not persisted anywhere, so there is nothing sensitive to leak
  into a chat message or into the logs.
* Admin actions are written to the audit collection with the acting admin ID.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.services import BotServices
from app.constants import ACTIVE_TASK_STATUSES, AccountStatus, TaskStatus
from app.core.humanize import format_bytes

logger = logging.getLogger(__name__)

router = Router(name="admin")

GIB = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# guard + stats
# ---------------------------------------------------------------------------
async def _require_admin_message(message: Message, services: BotServices) -> bool:
    if message.chat.type != "private":
        await message.reply("🔒 Operator controls are only available in a private chat.")
        return False
    if not services.is_admin(message.from_user.id):
        logger.warning("non-admin %s tried to use /admin", message.from_user.id)
        await message.reply("⛔ This command is restricted to operators.")
        return False
    return True


async def _require_admin_callback(callback: CallbackQuery, services: BotServices) -> bool:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("Operator controls only work in a private chat.", show_alert=True)
        return False
    if not services.is_admin(callback.from_user.id):
        logger.warning("non-admin %s tried an operator callback", callback.from_user.id)
        await callback.answer("This control is restricted to operators.", show_alert=True)
        return False
    return True


async def collect_stats(services: BotServices) -> Dict[str, Any]:
    """Aggregate service-wide numbers. Contains no per-user secrets."""
    repos = services.repos
    by_status = await repos.users.status_breakdown()
    task_stats = await repos.tasks.status_breakdown()
    users_total = sum(by_status.values())
    tasks_total = sum(task_stats.values())
    mongo_ok: Optional[bool] = None
    if services.mongo is not None:
        try:
            mongo_ok = await services.mongo.ping()
        except Exception:  # pragma: no cover - depends on live server
            mongo_ok = False
    return {
        "users": {"total": users_total, "by_status": by_status},
        "tasks": {
            "active": sum(int(task_stats.get(s, 0)) for s in ACTIVE_TASK_STATUSES),
            "failed": int(task_stats.get(TaskStatus.FAILED.value, 0)),
            "total": tasks_total,
            "by_status": task_stats,
        },
        "sessions": await repos.sessions.count(),
        "worker_count": services.userbots.worker_count,
        "max_active_userbots": int(services.settings.max_active_userbots),
        "worker_id": services.userbots.worker_id,
        "mongodb": mongo_ok,
    }


# ---------------------------------------------------------------------------
# /admin
# ---------------------------------------------------------------------------
@router.message(Command("admin"))
async def cmd_admin(message: Message, command: CommandObject, services: BotServices) -> None:
    if not await _require_admin_message(message, services):
        return
    args = (command.args or "").strip().split()

    if args and args[0] in ("health", "metrics"):
        await _send_health(message, services)
        return

    if args and args[0] in ("jobs", "tasks"):
        await _send_recent_jobs(message, services)
        return

    if args and args[0] == "user":
        if len(args) < 2:
            await message.answer("Usage: <code>/admin user &lt;telegram_id&gt;</code>")
            return
        await _send_user(message, services, args[1])
        return

    if args and args[0] == "quota":
        await _set_quota(message, services, args[1:])
        return

    if args and args[0] in ("ban", "unban"):
        if len(args) < 2:
            await message.answer(f"Usage: <code>/admin {args[0]} &lt;telegram_id&gt;</code>")
            return
        await _set_ban(message, services, args[1], banned=args[0] == "ban")
        return

    stats = await collect_stats(services)
    await message.answer(texts.admin_text(stats), reply_markup=kb.admin_menu())


@router.callback_query(F.data == "nav:admin")
async def cb_admin_entry(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    stats = await collect_stats(services)
    await callback.message.edit_text(texts.admin_text(stats), reply_markup=kb.admin_menu())
    await callback.answer()


@router.callback_query(F.data == "adm:home")
async def cb_admin_home(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    stats = await collect_stats(services)
    await callback.message.edit_text(texts.admin_text(stats), reply_markup=kb.admin_menu())
    await callback.answer("Refreshed.")


@router.callback_query(F.data == "adm:jobs")
async def cb_admin_jobs(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    tasks = await services.repos.tasks.list_by_status(list(TaskStatus), limit=10)
    await callback.message.edit_text(
        texts.tasks_text(tasks), reply_markup=kb.admin_menu()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:disconnect:"))
async def cb_admin_disconnect(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    target = _parse_id(callback.data.rsplit(":", 1)[-1])
    if target is None:
        await callback.answer("Bad user id.", show_alert=True)
        return
    await services.userbots.stop_user(target)
    await services.repos.users.set_status(target, AccountStatus.DISCONNECTED.value)
    await services.repos.audit.record(
        callback.from_user.id, "admin.session_disconnected", target_user_id=target
    )
    await services.notify(target, "🔌 An operator disconnected your userbot from this service.")
    await callback.answer(f"Disconnected {target}.", show_alert=True)


@router.callback_query(F.data.startswith("adm:purge:"))
async def cb_admin_purge(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    target = _parse_id(callback.data.rsplit(":", 1)[-1])
    if target is None:
        await callback.answer("Bad user id.", show_alert=True)
        return
    await services.userbots.stop_user(target)
    await services.sessions.delete_session(target)
    await services.repos.audit.record(
        callback.from_user.id, "admin.session_deleted", target_user_id=target
    )
    await callback.answer(f"Session deleted for {target}.", show_alert=True)


@router.callback_query(F.data.startswith("adm:ban:"))
async def cb_admin_ban(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    target = _parse_id(callback.data.rsplit(":", 1)[-1])
    if target is None:
        await callback.answer("Bad user id.", show_alert=True)
        return
    await _apply_ban(callback.from_user.id, target, True, services)
    await callback.answer(f"Banned {target}.", show_alert=True)


@router.callback_query(F.data.startswith("adm:unban:"))
async def cb_admin_unban(callback: CallbackQuery, services: BotServices) -> None:
    if not await _require_admin_callback(callback, services):
        return
    target = _parse_id(callback.data.rsplit(":", 1)[-1])
    if target is None:
        await callback.answer("Bad user id.", show_alert=True)
        return
    await _apply_ban(callback.from_user.id, target, False, services)
    await callback.answer(f"Unbanned {target}.", show_alert=True)


# ---------------------------------------------------------------------------
# message based operators
# ---------------------------------------------------------------------------
async def _send_health(message: Message, services: BotServices) -> None:
    snapshot = services.metrics.snapshot()
    lines = [
        "🩺 <b>Runtime health</b>",
        "",
        f"Uptime: {snapshot['uptime_seconds']}s",
        f"Worker id: <code>{html.escape(str(services.userbots.worker_id), quote=False)}</code>",
        f"Active accounts: {services.userbots.worker_count}",
    ]
    gauges = snapshot.get("gauges") or {}
    if gauges:
        lines += ["", "<b>Gauges</b>"]
        lines += [f"{key}: {value}" for key, value in sorted(gauges.items())]
    counters = snapshot.get("counters") or {}
    interesting = {
        key: value
        for key, value in counters.items()
        if key.startswith(("worker.", "manager.", "pipeline.", "login.", "session.", "telegram."))
    }
    if interesting:
        lines += ["", "<b>Counters</b>"]
        lines += [f"{key}: {value}" for key, value in sorted(interesting.items())]
    await message.answer("\n".join(lines), reply_markup=kb.admin_menu())


async def _send_recent_jobs(message: Message, services: BotServices) -> None:
    tasks = await services.repos.tasks.list_by_status(list(TaskStatus), limit=10)
    await message.answer(texts.tasks_text(tasks), reply_markup=kb.admin_menu())


async def _send_user(message: Message, services: BotServices, raw_id: str) -> None:
    target = _parse_id(raw_id)
    if target is None:
        await message.answer("That is not a valid Telegram user id.")
        return
    user = await services.repos.users.get(target)
    if user is None:
        await message.answer(f"No record for user <code>{target}</code>.")
        return
    history = await services.repos.audit.for_user(target, limit=5)
    worker = services.userbots.worker_for(target)
    extra: Dict[str, Any] = {"jobs": len(await services.repos.tasks.list_for_user(target, limit=50))}
    if worker is not None:
        snapshot = worker.snapshot()
        extra["uptime"] = None
        extra["worker"] = (
            f"queued={snapshot.queued} running={len(snapshot.active)} "
            f"done={snapshot.jobs_done} failed={snapshot.jobs_failed}"
        )
    body = texts.admin_user_text(user, extra)
    if history:
        body += "\n\n<b>Recent audit</b>"
        for row in history:
            action = html.escape(str(row.get("action", "")), quote=False)
            body += f"\n• {action}"
    if extra.get("worker"):
        body += f"\n\nWorker: {extra['worker']}"
    await message.answer(
        body, reply_markup=kb.admin_user_menu(target, banned=user.is_banned, connected=user.status == AccountStatus.CONNECTED.value)
    )


async def _set_ban(message: Message, services: BotServices, raw_id: str, *, banned: bool) -> None:
    target = _parse_id(raw_id)
    if target is None:
        await message.answer("That is not a valid Telegram user id.")
        return
    await _apply_ban(message.from_user.id, target, banned, services)
    await message.answer(
        f"{'🚫 Banned' if banned else '✅ Unbanned'} <code>{target}</code>.",
        reply_markup=kb.admin_menu(),
    )


async def _apply_ban(actor_id: int, target: int, banned: bool, services: BotServices) -> None:
    await services.repos.users.set_banned(target, banned)
    if banned:
        await services.userbots.stop_user(target)
        await services.repos.users.set_status(target, AccountStatus.DISCONNECTED.value)
    await services.repos.audit.record(
        actor_id, "admin.ban" if banned else "admin.unban", target_user_id=target
    )
    await services.notify(
        target,
        "🚫 Your access to this userbot service has been suspended by an operator."
        if banned
        else "✅ Your access to this userbot service has been restored.",
    )


async def _set_quota(message: Message, services: BotServices, args: List[str]) -> None:
    if len(args) < 3:
        await message.answer(
            "Usage: <code>/admin quota &lt;telegram_id&gt; &lt;storage_gb&gt; &lt;daily_jobs&gt;</code>\n"
            "Use 0 for a limit to mean <i>unlimited</i>."
        )
        return
    target = _parse_id(args[0])
    if target is None:
        await message.answer("That is not a valid Telegram user id.")
        return
    try:
        storage_gb = float(args[1])
        daily = int(args[2])
    except ValueError:
        await message.answer("Storage must be a number of GB and daily jobs an integer.")
        return
    if storage_gb < 0 or daily < 0:
        await message.answer("Limits cannot be negative.")
        return
    await services.repos.users.set_quota(
        target,
        storage_bytes=int(storage_gb * GIB),
        daily_tasks=daily,
    )
    await services.repos.audit.record(
        message.from_user.id,
        "admin.quota_updated",
        target_user_id=target,
        detail={"storage_bytes": int(storage_gb * GIB), "daily_tasks": daily},
    )
    await message.answer(
        f"📈 Quota updated for <code>{target}</code>: "
        f"storage {format_bytes(int(storage_gb * GIB))}, "
        f"daily jobs {daily or 'unlimited'}.",
        reply_markup=kb.admin_menu(),
    )


def _parse_id(raw: str) -> Optional[int]:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


__all__ = ["router", "collect_stats"]
