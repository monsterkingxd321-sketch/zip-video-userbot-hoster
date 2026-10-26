from __future__ import annotations

import pytest

from app.constants import StepStatus, TaskStatus
from app.db.repositories import Repositories
from app.userbot.pipeline import ZipTaskPipeline
from app.userbot.scheduler import AccountScheduler
from app.zipengine.extractor import ZipExtractor
from app.zipengine.safety import ZipLimits
from tests.fakes import FakeTransport
from tests.utils import make_zip


def extractor_for(settings):
    return ZipExtractor(
        ZipLimits(
            max_archive_bytes=settings.max_zip_size_bytes,
            max_total_uncompressed_bytes=settings.max_total_uncompressed_bytes,
            max_single_file_bytes=settings.max_single_file_bytes,
            max_entries=settings.max_zip_entries,
            max_compression_ratio=settings.max_compression_ratio,
            max_depth=settings.max_archive_depth,
            min_free_disk_bytes=0,
        ),
        nested=True,
        chunk_size=64 * 1024,
        max_videos_per_job=settings.max_videos_per_job,
    )


async def make_task(repos, *, user_id=20, message_id=500, mode="spoiler", archive_size=0):
    await repos.users.ensure_user(user_id, username="pipeline", first_name="Pipeline")
    await repos.users.set_status(user_id, "connected")
    task, _ = await repos.tasks.create_or_get(
        user_id=user_id,
        chat_id=-100123,
        message_id=message_id,
        mode=mode,
        peer={"peer_type": "channel", "spec": {"id": 77, "access_hash": 88}},
        archive={
            "name": "bundle.zip",
            "size": archive_size,
            "command_message_id": 700,
        },
        worker_id="worker-test",
    )
    return task


def pipeline_for(settings, repos, transport):
    scheduler = AccountScheduler(
        20,
        max_concurrency=2,
        max_retries=settings.max_retries,
        min_interval=0,
        base_delay=0,
        max_delay=0,
        uncertain_retries=settings.uncertain_send_retries,
    )
    return ZipTaskPipeline(
        settings=settings,
        repos=repos,
        transport=transport,
        scheduler=scheduler,
        extractor=extractor_for(settings),
        temp_root=settings.temp_path,
    )


@pytest.mark.asyncio
async def test_pipeline_extracts_nested_videos_sends_native_spoiler_and_cleans_up(
    repos, fake_settings, tmp_path
):
    inner = make_zip({"inner.mp4": b"inner-video-bytes"})
    archive = make_zip(
        {
            "videos/clip.mp4": b"clip-video-bytes",
            "notes.txt": b"not a video",
            "nested.zip": inner,
        }
    )
    task = await make_task(repos, archive_size=len(archive))
    transport = FakeTransport(archive=archive, file_name="bundle.zip")
    result = await pipeline_for(fake_settings, repos, transport).run(task)

    assert result.status == TaskStatus.COMPLETED.value
    assert len(transport.sent) == 2
    assert all(item["spoiler"] is True for item in transport.sent)
    assert all(item["supports_streaming"] for item in transport.sent)
    assert all(item["reply_to"] == 700 for item in transport.sent)
    assert len({item["random_id"] for item in transport.sent}) == 2
    assert sorted(item["path"].name for item in transport.sent) == ["clip.mp4", "inner.mp4"]
    assert [item["bytes"] for item in transport.sent] == sorted(
        [item["bytes"] for item in transport.sent]
    )

    stored = await repos.tasks.get(task.task_id)
    assert stored.status == TaskStatus.COMPLETED.value
    assert stored.stats["files_sent"] == 2
    assert stored.stats["files_total"] == 2
    assert stored.stats["files_skipped"] == 1
    assert stored.stats["archives_scanned"] == 2
    assert stored.stats["bytes_sent"] == len(b"clip-video-bytes") + len(b"inner-video-bytes")
    assert stored.stats["bytes_downloaded"] == len(archive)
    assert await repos.users.storage_usage(20) == 0
    assert (await repos.users.get(20)).usage["bytes_sent"] == stored.stats["bytes_sent"]

    steps = await repos.steps.list_for_task(task.task_id)
    assert len(steps) == 2
    assert all(step.status == StepStatus.SENT.value for step in steps)
    assert not (fake_settings.temp_path / "20" / task.task_id).exists()
    # Final progress text is the human summary, not an error.
    assert any("Sent 2/2" in text for text in transport.edits)


@pytest.mark.asyncio
async def test_pipeline_skips_already_sent_steps_on_resume(repos, fake_settings):
    archive = make_zip({"a.mp4": b"aaaa", "b.mp4": b"bbbb"})
    task = await make_task(repos, message_id=501, archive_size=len(archive))
    await repos.steps.ensure_steps(
        task.task_id,
        20,
        [
            {"rel_path": "a.mp4", "size": 4, "random_id": 123},
            {"rel_path": "b.mp4", "size": 4, "random_id": 456},
        ],
    )
    await repos.steps.mark_sent(task.task_id, "a.mp4", 900)

    transport = FakeTransport(archive=archive, file_name="bundle.zip")
    result = await pipeline_for(fake_settings, repos, transport).run(task)

    assert result.status == TaskStatus.COMPLETED.value
    assert [item["path"].name for item in transport.sent] == ["b.mp4"]
    completed_steps = await repos.steps.list_for_task(task.task_id)
    assert {step.rel_path: step.status for step in completed_steps} == {
        "a.mp4": StepStatus.SENT.value,
        "b.mp4": StepStatus.SENT.value,
    }
    assert (await repos.tasks.get(task.task_id)).stats["files_sent"] == 2


@pytest.mark.asyncio
async def test_pipeline_rejects_non_zip_and_cleans_up(repos, fake_settings):
    task = await make_task(repos, message_id=502)
    transport = FakeTransport(archive=b"not a zip", file_name="not-a-zip.txt")
    result = await pipeline_for(fake_settings, repos, transport).run(task)
    assert result.status == TaskStatus.FAILED.value
    assert result.error["code"] == "NOT_A_ZIP"
    assert not transport.sent
    assert not (fake_settings.temp_path / "20" / task.task_id).exists()
    assert await repos.users.storage_usage(20) == 0
