from __future__ import annotations

from app.bot import texts
from app.db.models import TaskRecord, UserRecord


def test_dynamic_telegram_names_and_errors_are_html_escaped():
    user = UserRecord(
        user_id=1,
        username="<b>user</b>",
        first_name="<script>alert(1)</script>",
        status="connected",
        account={"username": "<admin>", "fingerprint": "<fp>", "phone_masked": "<phone>"},
    )
    assert "&lt;admin&gt;" in texts.status_line(user)
    assert "&lt;bad&gt;" in texts.status_line(
        UserRecord(user_id=1, status="<bad>")
    )
    admin = texts.admin_user_text(user)
    assert "<script>" not in admin
    assert "&lt;script&gt;" in admin
    assert "&lt;fp&gt;" in admin
    assert "&lt;phone&gt;" in admin

    connected = texts.connected_text(
        {"username": "<name>", "first_name": "<first>"}, fingerprint="<fp>"
    )
    assert "<name>" not in connected
    assert "&lt;name&gt;" in connected
    assert "&lt;fp&gt;" in connected

    task = TaskRecord(
        task_id="a" * 24,
        user_id=1,
        chat_id=2,
        message_id=3,
        dedupe_key="k",
        status="<status>",
        error={"code": "<code>", "message": "<img src=x onerror=alert(1)>"},
    )
    detail = texts.task_detail_text(task)
    assert "<img" not in detail
    assert "&lt;img" in detail
    assert "&lt;code&gt;" in detail
    assert "&lt;status&gt;" in detail


def test_connect_prompts_require_manual_phone_entry_and_explain_delivery():
    phone_prompt = texts.connect_phone_prompt().lower()
    assert "manually" in phone_prompt
    assert "contact sharing is disabled" in phone_prompt
    assert "+919876543210" in phone_prompt

    code_prompt = texts.connect_code_prompt().lower()
    assert "resend code" in code_prompt
    assert "sms" in code_prompt

    assert "telegram app" in texts.code_delivery_text("app").lower()
    assert "sms" in texts.code_delivery_text("sms").lower()
    assert "email" in texts.code_delivery_text("email").lower()
    assert "secret word" in texts.code_delivery_text("sms_word").lower()
    assert "secret phrase" in texts.code_delivery_text("sms_phrase").lower()


def test_admin_status_breakdown_is_escaped():
    rendered = texts.admin_text(
        {
            "users": {"total": 1, "by_status": {"<bad>": 1}},
            "tasks": {},
            "sessions": 0,
            "worker_count": 0,
            "max_active_userbots": 1,
        }
    )
    assert "<bad>" not in rendered
    assert "&lt;bad&gt;" in rendered
