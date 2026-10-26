from __future__ import annotations

import pytest

from app.userbot.scheduler import AccountScheduler
from app.userbot.transport import (
    FloodWaitSignal,
    PermanentTransportError,
    TransientTransportError,
    UncertainSendError,
)


def make_clock():
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += float(seconds)

    return now, sleep, lambda: now[0]


@pytest.mark.asyncio
async def test_flood_wait_pauses_account_and_retries():
    now, sleep, clock = make_clock()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitSignal(5)
        return "ok"

    scheduler = AccountScheduler(
        1,
        max_retries=2,
        sleep=sleep,
        clock=clock,
        rand=lambda: 0.0,
        flood_extra=0,
    )
    assert await scheduler.execute("test", factory) == "ok"
    assert calls["n"] == 2
    assert scheduler.stats.flood_waits == 1
    assert now[0] == 5
    assert scheduler.paused_for == 0


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried_when_not_idempotent():
    now, sleep, clock = make_clock()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise UncertainSendError("already delivered?")

    scheduler = AccountScheduler(1, sleep=sleep, clock=clock, uncertain_retries=3)
    with pytest.raises(UncertainSendError):
        await scheduler.execute("send_text", factory, idempotent=False)
    assert calls["n"] == 1
    assert scheduler.stats.uncertain_errors == 1


@pytest.mark.asyncio
async def test_uncertain_idempotent_retry_uses_same_attempt_budget():
    now, sleep, clock = make_clock()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise UncertainSendError("maybe")
        return "delivered"

    scheduler = AccountScheduler(
        1,
        sleep=sleep,
        clock=clock,
        uncertain_retries=1,
        base_delay=0,
        max_delay=0,
        rand=lambda: 0.0,
    )
    assert await scheduler.execute("send_video", factory, idempotent=True) == "delivered"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_transient_retry_is_disabled_for_non_idempotent_operations():
    now, sleep, clock = make_clock()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise TransientTransportError("network")

    scheduler = AccountScheduler(
        1, sleep=sleep, clock=clock, max_retries=4, base_delay=0, max_delay=0
    )
    with pytest.raises(TransientTransportError):
        await scheduler.execute("send_text", factory, idempotent=False)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_permanent_errors_are_never_retried():
    now, sleep, clock = make_clock()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise PermanentTransportError("CHAT_WRITE_FORBIDDEN", "no")

    scheduler = AccountScheduler(1, sleep=sleep, clock=clock, max_retries=5)
    with pytest.raises(PermanentTransportError):
        await scheduler.execute("send_video", factory)
    assert calls["n"] == 1
    assert scheduler.stats.permanent_errors == 1


@pytest.mark.asyncio
async def test_min_interval_is_applied_between_successful_requests():
    now, sleep, clock = make_clock()
    scheduler = AccountScheduler(1, sleep=sleep, clock=clock, min_interval=2)

    async def factory():
        return "ok"

    await scheduler.execute("one", factory)
    await scheduler.execute("two", factory)
    assert now[0] >= 2
