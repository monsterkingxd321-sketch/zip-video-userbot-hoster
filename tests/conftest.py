"""Shared pytest fixtures (no live Telegram or MongoDB server required)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient

from app.db.repositories import Repositories


@pytest_asyncio.fixture
async def repos():
    """Repositories backed by an in-memory MongoDB-compatible database."""
    client = AsyncMongoMockClient()
    db = client["zip_userbot_test"]
    return Repositories.create(db, default_storage_quota_bytes=0, default_daily_task_quota=0)


@pytest.fixture
def fake_settings(tmp_path):
    """The subset of Settings used by the pipeline and extractor."""
    return SimpleNamespace(
        temp_path=tmp_path / "tmp",
        upload_concurrency=2,
        download_concurrency=2,
        extract_concurrency=2,
        max_retries=2,
        min_seconds_between_requests=0.0,
        retry_base_delay=0.0,
        retry_max_delay=0.01,
        floodwait_extra_seconds=0.0,
        uncertain_send_retries=0,
        progress_update_interval=0.01,
        download_chunk_size_kb=64,
        max_zip_size_bytes=10 * 1024 * 1024,
        max_total_uncompressed_bytes=20 * 1024 * 1024,
        max_single_file_bytes=5 * 1024 * 1024,
        max_zip_entries=100,
        max_compression_ratio=0.0,
        max_archive_depth=3,
        max_videos_per_job=50,
        min_free_disk_bytes=0,
        streamable_extension_set=(".mp4", ".m4v", ".mov"),
        session_lease_seconds=120,
        task_timeout_seconds=60,
        default_spoiler=False,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
