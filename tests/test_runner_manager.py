from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.metrics import Metrics
from app.userbot.runner import UserbotManager


@pytest.mark.asyncio
async def test_ensure_releases_session_lock_when_transport_cannot_be_created():
    locks = SimpleNamespace(
        acquire=AsyncMock(return_value=True),
        release=AsyncMock(return_value=True),
        holder=AsyncMock(return_value=None),
    )
    repos = SimpleNamespace(
        users=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(is_banned=False, status="connected"))
        ),
        locks=locks,
    )
    sessions = SimpleNamespace(create_transport=AsyncMock(return_value=None))
    manager = object.__new__(UserbotManager)
    manager._repos = repos
    manager._sessions = sessions
    manager._settings = SimpleNamespace(
        max_active_userbots=5,
        idle_disconnect_seconds=0,
        session_lease_seconds=120,
    )
    manager._workers = {}
    manager._lock = asyncio.Lock()
    manager._worker_id = "test-worker"
    manager._metrics = Metrics()

    assert await manager.ensure(123) is None
    locks.release.assert_awaited_once_with(123, "test-worker")


@pytest.mark.asyncio
async def test_ensure_does_not_start_a_banned_account():
    locks = SimpleNamespace(
        acquire=AsyncMock(return_value=True),
        release=AsyncMock(return_value=True),
        holder=AsyncMock(return_value=None),
    )
    repos = SimpleNamespace(
        users=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(is_banned=True))),
        locks=locks,
    )
    manager = object.__new__(UserbotManager)
    manager._repos = repos
    manager._sessions = SimpleNamespace(create_transport=AsyncMock())
    manager._settings = SimpleNamespace(
        max_active_userbots=5,
        idle_disconnect_seconds=0,
        session_lease_seconds=120,
    )
    manager._workers = {}
    manager._lock = asyncio.Lock()
    manager._worker_id = "test-worker"
    manager._metrics = Metrics()

    assert await manager.ensure(123) is None
    locks.acquire.assert_not_awaited()
