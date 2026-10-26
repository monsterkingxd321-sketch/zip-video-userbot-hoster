"""User facing copy for the control bot (kept in one place for consistency)."""

from __future__ import annotations

import html
from typing import Any, Dict, Optional

from app.constants import AccountStatus
from app.core.humanize import format_bytes, format_duration
from app.db.models import TaskRecord, UserRecord

STATUS_LABELS: Dict[str, str] = {
    AccountStatus.NEW.value: "not connected",
    AccountStatus.AWAITING_CODE.value: "waiting for the login code",
    AccountStatus.AWAITING_PASSWORD.value: "waiting for the 2FA password",
    AccountStatus.CONNECTED.value: "connected",
    AccountStatus.DISCONNECTED.value: "disconnected",
    AccountStatus.REVOKED.value: "session revoked by Telegram",
    AccountStatus.ERROR.value: "connection error",
}

TASK_LABELS: Dict[str, str] = {
    "queued": "queued",
    "downloading": "downloading",
    "extracting": "extracting",
    "sending": "uploading videos",
    "completed": "completed",
    "partial": "completed with errors",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def _esc(value: Any) -> str:
    """Escape any value that came from Telegram, MongoDB or an exception."""
    return html.escape(str(value if value is not None else ""), quote=False)

WELCOME = (
    "👋 <b>ZIP → Video Userbot Hosting</b>\n\n"
    "This bot lets you attach <b>your own</b> Telegram account and turn ZIP archives "
    "into native Telegram videos.\n\n"
    "<b>How it works</b>\n"
    "1. Connect your Telegram account (phone number + login code).\n"
    "2. In any chat, reply to a <code>.zip</code> file with <code>/unzip</code>.\n"
    "3. Every video inside the archive is uploaded as a native Telegram video.\n"
    "   Add <code>spoiler</code> to send them with the spoiler effect.\n\n"
    "<b>Commands</b>\n"
    "<code>/unzip</code> – extract and send videos\n"
    "<code>/unzip spoiler</code> – send videos with the spoiler effect\n"
    "<code>/cancel</code> – cancel the latest job\n"
    "<code>/cancel all</code> – cancel every queued job\n"
)

SECURITY_NOTICE = (
    "🔐 <b>Before you connect — please read</b>\n\n"
    "• Connecting an account gives this service the ability to act <b>as you</b> "
    "through Telegram's MTProto API, within the permissions your account already has.\n"
    "• Your <b>login code</b> and <b>two‑step password</b> are used once, in memory only. "
    "They are never written to a database, never written to disk and never logged.\n"
    "• Your session key is encrypted before it is stored, on a private volume and as an "
    "encrypted backup. Encryption keys come from the server environment, never from code.\n"
    "• You can revoke everything at any time: <b>Disconnect Account</b> stops the service, "
    "<b>Delete Session</b> destroys the stored key. You can also terminate the session "
    "yourself in Telegram → Settings → Devices.\n"
    "• <b>Never</b> send a login code or password in a group. This bot only accepts them "
    "in this private chat.\n\n"
    "Do you understand and consent?"
)

SPOILER_HELP = (
    "🫥 <b>Spoiler mode</b>\n\n"
    "Reply to a <code>.zip</code> with:\n"
    "<code>/unzip spoiler</code>\n\n"
    "Every extracted video is sent with Telegram's native spoiler flag, so it stays "
    "hidden until the viewer taps it. You can make spoiler mode the default in Settings."
)


def status_line(user: Optional[UserRecord]) -> str:
    if user is None:
        return "unknown"
    label = STATUS_LABELS.get(user.status, _esc(user.status))
    if user.status == AccountStatus.CONNECTED.value and user.account.get("username"):
        return f"{label} (@{_esc(user.account['username'])})"
    return label


def quota_line(user: UserRecord) -> str:
    storage_limit = int(user.quota.get("storage_bytes", 0) or 0)
    used = int(user.usage.get("storage_bytes", 0) or 0)
    daily_limit = int(user.quota.get("daily_tasks", 0) or 0)
    tasks_today = int(user.usage.get("tasks_today", 0) or 0)
    storage = (
        f"{format_bytes(used)} / {format_bytes(storage_limit)}"
        if storage_limit > 0
        else f"{format_bytes(used)} (unlimited)"
    )
    daily = f"{tasks_today} / {daily_limit}" if daily_limit > 0 else f"{tasks_today} (unlimited)"
    return f"Storage: {storage}\nJobs today: {daily}"


def dashboard_text(user: UserRecord, *, is_admin: bool = False) -> str:
    settings = user.settings or {}
    spoiler = "on" if settings.get("spoiler_default") else "off"
    notify = "on" if settings.get("notify_on_complete", True) else "off"
    lines = [
        "🎛 <b>Your userbot dashboard</b>",
        "",
        f"<b>Account</b>: {status_line(user)}",
        f"<b>Consent</b>: {'given' if user.consent_given else 'not given'}",
        f"<b>Spoiler mode by default</b>: {spoiler}",
        f"<b>Completion notices</b>: {notify}",
        "",
        quota_line(user),
    ]
    if is_admin:
        lines += ["", "🛡 You are an operator: use /admin for service controls."]
    return "\n".join(lines)


def task_line(task: TaskRecord, index: int) -> str:
    label = TASK_LABELS.get(task.status, _esc(task.status))
    sent = task.files_sent
    total = task.files_total
    when = task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "-"
    extra = f" · {sent}/{total} video(s)" if total else ""
    return f"{index}. <code>{_esc(task.task_id[:8])}</code> · {label}{extra} · {when}"


def tasks_text(tasks) -> str:
    if not tasks:
        return "📭 No jobs yet.\n\nReply to a <code>.zip</code> with <code>/unzip</code> to start one."
    lines = ["🗂 <b>Recent jobs</b>", ""]
    lines += [task_line(task, index) for index, task in enumerate(tasks, start=1)]
    lines += ["", "Use <code>/cancel</code> in the chat where the job is running to stop it."]
    return "\n".join(lines)


def task_detail_text(task: TaskRecord) -> str:
    label = TASK_LABELS.get(task.status, _esc(task.status))
    lines = [
        f"🧾 <b>Job {_esc(task.task_id[:8])}</b>",
        "",
        f"Status: <b>{label}</b>",
        f"Mode: {'spoiler' if task.spoiler else 'normal'}",
        f"Videos: {task.files_sent}/{task.files_total} sent",
    ]
    if task.stats.get("files_failed"):
        lines.append(f"Failed: {task.stats['files_failed']}")
    if task.stats.get("files_uncertain"):
        lines.append(
            f"Uncertain: {task.stats['files_uncertain']} "
            "(delivery could not be confirmed; rerun with <code>/unzip retry</code>)"
        )
    if task.stats.get("bytes_sent"):
        lines.append(f"Uploaded: {format_bytes(task.stats['bytes_sent'])}")
    if task.error:
        lines.append("")
        lines.append(
            f"Error: <code>{_esc(task.error.get('code', ''))}</code> "
            f"{_esc(task.error.get('message', ''))}"
        )
    if task.created_at:
        lines.append("")
        lines.append(f"Created: {task.created_at:%Y-%m-%d %H:%M:%S} UTC")
    return "\n".join(lines)


def admin_text(stats: Dict[str, Any]) -> str:
    users = stats.get("users", {})
    tasks = stats.get("tasks", {})
    lines = [
        "🛡 <b>Service status</b>",
        "",
        f"<b>Users</b>: {users.get('total', 0)} total",
    ]
    breakdown = users.get("by_status") or {}
    connected = int(breakdown.get(AccountStatus.CONNECTED.value, 0))
    lines.append(f"<b>Connected accounts</b>: {connected}")
    lines.append(
        "Status breakdown: "
        + ", ".join(f"{_esc(k)}={int(v)}" for k, v in sorted(breakdown.items()))
        if breakdown
        else "Status breakdown: none yet"
    )
    lines += [
        "",
        f"<b>Active jobs</b>: {tasks.get('active', 0)}",
        f"<b>Failed jobs</b>: {tasks.get('failed', 0)}",
        f"<b>Sessions stored</b>: {stats.get('sessions', 0)}",
        "",
        f"<b>Workers</b>: {stats.get('worker_count', 0)} / "
        f"{stats.get('max_active_userbots', 0)}",
    ]
    mongo = stats.get("mongodb")
    if mongo is not None:
        lines.append(f"<b>MongoDB</b>: {'ok' if mongo else 'unreachable'}")
    lines += [
        "",
        "<b>Commands</b>",
        "<code>/admin user &lt;id&gt;</code> – inspect a user",
        "<code>/admin jobs</code> – recent jobs across all users",
        "<code>/admin health</code> – runtime metrics",
    ]
    return "\n".join(lines)


def admin_user_text(user: UserRecord, extra: Optional[Dict[str, Any]] = None) -> str:
    extra = extra or {}
    lines = [
        f"👤 <b>User {int(user.user_id)}</b>",
        "",
        f"Name: {_esc(user.display_name)}",
        f"Status: {status_line(user)}",
        f"Banned: {'yes' if user.is_banned else 'no'}",
        f"Consent: {'given' if user.consent_given else 'no'}",
        "",
        quota_line(user),
    ]
    fingerprint = (user.account or {}).get("fingerprint")
    if fingerprint:
        lines.append(f"Session fingerprint: <code>{_esc(fingerprint)}</code>")
    masked = (user.account or {}).get("phone_masked")
    if masked:
        lines.append(f"Phone: <code>{_esc(masked)}</code>")
    if extra.get("jobs"):
        lines += ["", f"Recent jobs: {extra['jobs']}"]
    if extra.get("uptime") is not None:
        lines.append(f"Worker uptime: {format_duration(extra['uptime'])}")
    return "\n".join(lines)


def connect_phone_prompt() -> str:
    return (
        "📱 Send your phone number in <b>international format</b>, for example "
        "<code>+919876543210</code>.\n\n"
        "Telegram will send the login code to your Telegram app (not by SMS), and you "
        "will paste it here.\n\n"
        "Send /start to cancel."
    )


def connect_code_prompt() -> str:
    return (
        "✉️ Enter the <b>login code</b> Telegram just sent you.\n\n"
        "Spaces are fine — <code>1 2 3 4 5</code> works exactly like <code>12345</code>.\n"
        "I will try to delete your message so the code does not stay in this chat."
    )


def connect_password_prompt() -> str:
    return (
        "🔑 Your account uses <b>two-step verification</b>.\n\n"
        "Send your Telegram password here. It is used once, in memory, and is never "
        "stored or logged.\n\n"
        "⚠️ Consider deleting the message yourself as well if you are unsure about this "
        "chat's history."
    )


def connected_text(account: Dict[str, Any], *, fingerprint: Optional[str] = None) -> str:
    name = account.get("username")
    who = f"@{_esc(name)}" if name else _esc(account.get("first_name") or account.get("user_id", ""))
    lines = [
        "✅ <b>Account connected</b>",
        "",
        f"Signed in as <b>{who}</b>.",
        "Your userbot is active now: reply to a <code>.zip</code> with <code>/unzip</code> "
        "in any chat you have access to.",
    ]
    if fingerprint:
        lines += ["", f"Session fingerprint: <code>{_esc(fingerprint)}</code>"]
    lines += [
        "",
        "You can stop the service at any time from this dashboard.",
    ]
    return "\n".join(lines)


__all__ = [
    "WELCOME",
    "SECURITY_NOTICE",
    "SPOILER_HELP",
    "STATUS_LABELS",
    "TASK_LABELS",
    "dashboard_text",
    "tasks_text",
    "task_detail_text",
    "admin_text",
    "admin_user_text",
    "connect_phone_prompt",
    "connect_code_prompt",
    "connect_password_prompt",
    "connected_text",
    "status_line",
    "quota_line",
]
