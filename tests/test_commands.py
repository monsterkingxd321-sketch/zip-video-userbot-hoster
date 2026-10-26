from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.metrics import Metrics
from app.userbot.commands import (
    is_cancel_command,
    is_unzip_command,
    parse_cancel_command,
    parse_unzip_command,
)
from app.userbot.runner import UserbotWorker
from app.userbot.transport import AccountInfo, CommandContext


def test_unzip_parser_is_strict_about_flags():
    assert is_unzip_command("/unzip@somebot spoiler")
    parsed = parse_unzip_command("/uz spoiler retry unknown")
    assert parsed is not None
    assert parsed.spoiler is True
    assert parsed.retry is True
    assert parsed.flags == ("unknown",)
    assert parse_unzip_command("/notunzip") is None
    assert parse_unzip_command("/unzip nos", default_spoiler=True).spoiler is False


def test_cancel_parser():
    assert is_cancel_command("/cancel@bot")
    parsed = parse_cancel_command("/cancel all")
    assert parsed is not None and parsed.scope == "all"
    assert parse_cancel_command("/cancel 123").scope == "123"


def _bare_worker():
    worker = object.__new__(UserbotWorker)
    worker.user_id = 10
    worker.account = AccountInfo(user_id=20, username="owner")
    worker._metrics = Metrics()
    worker.touch = lambda: None
    worker._handle_unzip = AsyncMock()
    worker._handle_cancel = AsyncMock()
    return worker


@pytest.mark.asyncio
async def test_userbot_commands_require_own_outgoing_account():
    worker = _bare_worker()
    foreign = CommandContext(
        chat_id=1,
        message_id=2,
        sender_id=99,
        text="/unzip",
        is_outgoing=False,
    )
    await worker.handle_command(foreign)
    worker._handle_unzip.assert_not_awaited()

    wrong_sender = CommandContext(
        chat_id=1,
        message_id=2,
        sender_id=99,
        text="/unzip",
        is_outgoing=True,
    )
    await worker.handle_command(wrong_sender)
    worker._handle_unzip.assert_not_awaited()

    owned = CommandContext(
        chat_id=1,
        message_id=2,
        sender_id=20,
        text="/unzip",
        is_outgoing=True,
    )
    await worker.handle_command(owned)
    worker._handle_unzip.assert_awaited_once_with(owned)
