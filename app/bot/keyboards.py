"""Inline keyboards for the control bot dashboard."""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import AccountStatus
from app.db.models import TaskRecord, UserRecord


def main_menu(user: Optional[UserRecord], *, is_admin: bool = False) -> InlineKeyboardMarkup:
    status = user.status if user else AccountStatus.NEW.value
    connected = status == AccountStatus.CONNECTED.value
    rows: list[list[InlineKeyboardButton]] = []

    if connected:
        rows.append([InlineKeyboardButton(text="🔌 Disconnect Account", callback_data="act:disconnect")])
        rows.append([InlineKeyboardButton(text="🗑 Delete Session", callback_data="act:delete")])
    else:
        rows.append([InlineKeyboardButton(text="➕ Connect Account", callback_data="act:connect")])

    rows.append(
        [
            InlineKeyboardButton(text="📊 Status", callback_data="nav:status"),
            InlineKeyboardButton(text="🗂 Jobs", callback_data="nav:tasks"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="⚙️ Settings", callback_data="nav:settings"),
            InlineKeyboardButton(text="🫥 Spoiler help", callback_data="nav:spoiler"),
        ]
    )
    rows.append([InlineKeyboardButton(text="🔐 Security", callback_data="nav:security")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛡 Admin", callback_data="nav:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def consent_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ I understand and consent", callback_data="act:consent")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="act:cancel_login")]]
    )


def code_keyboard() -> InlineKeyboardMarkup:
    """Login-code actions: resend normally, force SMS, or cancel."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Resend code", callback_data="act:resend_code")],
            [
                InlineKeyboardButton(
                    text="📩 Send code by SMS instead", callback_data="act:resend_sms"
                )
            ],
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="act:cancel_login")],
        ]
    )


def settings_menu(user: UserRecord) -> InlineKeyboardMarkup:
    settings = user.settings or {}
    spoiler = "🟢 on" if settings.get("spoiler_default") else "⚪️ off"
    notify = "🟢 on" if settings.get("notify_on_complete", True) else "⚪️ off"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🫥 Spoiler default: {spoiler}", callback_data="set:spoiler"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🔔 Completion notices: {notify}", callback_data="set:notify"
                )
            ],
            [InlineKeyboardButton(text="🧹 Cancel all my jobs", callback_data="act:cancel_all")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
        ]
    )


def tasks_menu(tasks) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in list(tasks)[:5]:
        label = "🟢" if task.is_active() else "⚪️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} {task.task_id[:8]} · {task.status}",
                    callback_data=f"job:{task.task_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="nav:tasks")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_menu(task: TaskRecord) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if task.is_active():
        rows.append(
            [InlineKeyboardButton(text="🚫 Cancel this job", callback_data=f"jcancel:{task.task_id}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:tasks")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def security_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete Session", callback_data="act:delete")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
        ]
    )


def back_home_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")]]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:home")],
            [InlineKeyboardButton(text="🗂 Recent jobs", callback_data="adm:jobs")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
        ]
    )


def admin_user_menu(user_id: int, *, banned: bool, connected: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if connected:
        rows.append(
            [InlineKeyboardButton(text="🔌 Disconnect", callback_data=f"adm:disconnect:{user_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Unban" if banned else "🚫 Ban",
                callback_data=f"adm:{'unban' if banned else 'ban'}:{user_id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="🗑 Delete session", callback_data=f"adm:purge:{user_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Admin", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "main_menu",
    "consent_menu",
    "cancel_keyboard",
    "code_keyboard",
    "settings_menu",
    "tasks_menu",
    "task_menu",
    "security_menu",
    "back_home_menu",
    "admin_menu",
    "admin_user_menu",
]
