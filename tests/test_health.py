from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.metrics import Metrics
from app.health import HealthServer


class FakeMongo:
    def __init__(self, ok: bool):
        self.ok = ok

    async def ping(self):
        return self.ok


class FakeUserbots:
    def snapshot(self):
        return {"workers": 2, "queued": 1}


@pytest.mark.asyncio
async def test_health_ready_and_metrics_endpoints():
    app = HealthServer(
        host="127.0.0.1",
        port=0,
        mongo=FakeMongo(True),
        metrics=Metrics(),
        userbots=FakeUserbots(),
        version="test-1",
    ).build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        health = await client.get("/healthz")
        assert health.status == 200
        assert (await health.json())["status"] == "ok"

        ready = await client.get("/readyz")
        assert ready.status == 200
        assert (await ready.json())["mongodb"] is True

        metrics = await client.get("/metrics")
        assert metrics.status == 200
        payload = await metrics.json()
        assert payload["version"] == "test-1"
        assert payload["userbots"]["workers"] == 2
        assert "session" not in str(payload).lower()

        root = await client.get("/")
        assert root.status == 200
        assert "/healthz" in await root.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ready_returns_503_when_mongo_is_down():
    app = HealthServer(
        host="127.0.0.1",
        port=0,
        mongo=FakeMongo(False),
        version="test-1",
    ).build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ready = await client.get("/readyz")
        assert ready.status == 503
        assert (await ready.json())["status"] == "degraded"
    finally:
        await client.close()
