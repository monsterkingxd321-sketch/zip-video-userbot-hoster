from __future__ import annotations

from app.constants import AccountStatus, StepStatus, TaskStatus
from app.db.models import utcnow


async def _user(repos, user_id=10):
    await repos.users.ensure_user(user_id, username="u", first_name="User")
    await repos.users.set_status(user_id, AccountStatus.CONNECTED.value)
    return await repos.users.get(user_id)


async def _task(repos, user_id=10, message_id=100, mode="plain", attempt=0):
    task, _created = await repos.tasks.create_or_get(
        user_id=user_id,
        chat_id=-100,
        message_id=message_id,
        mode=mode,
        peer={"peer_type": "channel", "spec": {"id": 1}},
        archive={"name": "a.zip", "size": 100},
        worker_id="worker-1",
        attempt=attempt,
    )
    return task


async def test_daily_quota_rollover_and_refund(repos):
    user = await _user(repos)
    assert user is not None
    await repos.users.set_quota(10, storage_bytes=1000, daily_tasks=2)

    ok, used, limit = await repos.users.consume_daily_task_quota(10, "2026-01-01")
    assert (ok, used, limit) == (True, 1, 2)
    ok, used, _ = await repos.users.consume_daily_task_quota(10, "2026-01-01")
    assert (ok, used) == (True, 2)
    ok, used, _ = await repos.users.consume_daily_task_quota(10, "2026-01-01")
    assert (ok, used) == (False, 2)

    await repos.users.refund_daily_task_quota(10)
    ok, used, _ = await repos.users.consume_daily_task_quota(10, "2026-01-01")
    assert (ok, used) == (True, 2)

    ok, used, _ = await repos.users.consume_daily_task_quota(10, "2026-01-02")
    assert (ok, used) == (True, 1)
    updated = await repos.users.get(10)
    assert updated.usage["tasks_today"] == 1
    assert updated.usage["total_tasks"] >= 3


async def test_storage_reservation_never_goes_negative(repos):
    await _user(repos)
    await repos.users.set_quota(10, storage_bytes=10, daily_tasks=0)

    allowed, used, limit = await repos.users.reserve_storage(10, 8)
    assert (allowed, used, limit) == (True, 8, 10)
    allowed, used, _ = await repos.users.reserve_storage(10, 3)
    assert (allowed, used) == (False, 8)

    await repos.users.release_storage(10, 5)
    assert await repos.users.storage_usage(10) == 3
    allowed, used, _ = await repos.users.reserve_storage(10, 7)
    assert (allowed, used) == (True, 10)

    await repos.users.release_storage(10, 999)
    assert await repos.users.storage_usage(10) == 0


async def test_stale_storage_reservations_are_cleared_on_startup(repos):
    await _user(repos)
    await _user(repos, user_id=11)
    await repos.users.set_quota(10, storage_bytes=1000, daily_tasks=0)
    await repos.users.set_quota(11, storage_bytes=1000, daily_tasks=0)
    await repos.users.reserve_storage(10, 100)
    await repos.users.reserve_storage(11, 200)

    assert await repos.users.reset_all_storage_usage() == 2
    assert await repos.users.storage_usage(10) == 0
    assert await repos.users.storage_usage(11) == 0


async def test_task_dedupe_and_started_at_preserved(repos):
    await _user(repos)
    first = await _task(repos, message_id=101)
    duplicate = await _task(repos, message_id=101)
    assert duplicate.task_id == first.task_id

    await repos.tasks.set_status(first.task_id, TaskStatus.DOWNLOADING.value, worker_id="worker-1")
    started = (await repos.tasks.get(first.task_id)).started_at
    assert started is not None
    await repos.tasks.set_status(first.task_id, TaskStatus.SENDING.value, worker_id="worker-1")
    assert (await repos.tasks.get(first.task_id)).started_at == started

    await repos.tasks.set_status(first.task_id, TaskStatus.COMPLETED.value)
    assert (await repos.tasks.get(first.task_id)).active is False
    again = await _task(repos, message_id=101)
    assert again.task_id != first.task_id


async def test_step_claim_is_atomic_and_uncertain_after_crash(repos):
    await _user(repos)
    task = await _task(repos, message_id=102)
    created = await repos.steps.ensure_steps(
        task.task_id,
        10,
        [
            {"rel_path": "a.mp4", "size": 5, "random_id": 1},
            {"rel_path": "b.mp4", "size": 5, "random_id": 2},
        ],
    )
    assert created == 2
    step = await repos.steps.claim(task.task_id, "a.mp4")
    assert step is not None
    assert step.status == StepStatus.SENDING.value
    assert await repos.steps.claim(task.task_id, "a.mp4") is None
    await repos.steps.mark_sent(task.task_id, "a.mp4", 999)
    assert await repos.steps.claim(task.task_id, "a.mp4") is None

    await repos.steps.claim(task.task_id, "b.mp4")
    assert await repos.steps.unstick_inflight(task.task_id) == 1
    counts = await repos.steps.counts(task.task_id)
    assert counts[StepStatus.SENT.value] == 1
    assert counts[StepStatus.UNCERTAIN.value] == 1


async def test_interrupt_stale_only_touches_active_tasks(repos):
    await _user(repos)
    active = await _task(repos, message_id=103)
    done = await _task(repos, message_id=104)
    await repos.tasks.set_status(active.task_id, TaskStatus.DOWNLOADING.value, worker_id="dead")
    await repos.tasks.set_status(done.task_id, TaskStatus.COMPLETED.value, worker_id="dead")

    interrupted = await repos.tasks.interrupt_stale([], utcnow())
    assert active.task_id in interrupted
    assert (await repos.tasks.get(active.task_id)).status == TaskStatus.INTERRUPTED.value
    assert (await repos.tasks.get(done.task_id)).status == TaskStatus.COMPLETED.value

    assert await repos.tasks.requeue_interrupted(active.task_id) is True
    requeued = await repos.tasks.get(active.task_id)
    assert requeued.status == TaskStatus.QUEUED.value
    assert requeued.active is True
    assert requeued.worker_id is None
