"""HTTP health and metrics endpoint (Railway health checks).

Endpoints
---------
``GET /healthz``
    Liveness. Answers ``200`` as long as the process is running, which is what
    Railway needs to decide whether to restart the container.
``GET /readyz``
    Readiness. Answers ``200`` only when MongoDB answers a ``ping``; otherwise
    ``503`` so a rolling deploy does not route traffic to a broken replica.
``GET /metrics``
    Structured metrics: process counters/gauges, worker pool state and
    collection-level counts. Deliberately contains **no** per-user data, no
    session material and no credentials.
``GET /``
    A one-line banner so the Railway URL is self-describing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


class HealthServer:
    """Small aiohttp application bound to ``PORT`` (Railway) or ``HEALTH_PORT``."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        mongo: Optional[Any] = None,
        metrics: Optional[Any] = None,
        userbots: Optional[Any] = None,
        repos: Optional[Any] = None,
        version: str = "1.0.0",
    ) -> None:
        self._host = host
        self._port = int(port)
        self._mongo = mongo
        self._metrics = metrics
        self._userbots = userbots
        self._repos = repos
        self._version = version
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/healthz", self._handle_health)
        app.router.add_get("/readyz", self._handle_ready)
        app.router.add_get("/metrics", self._handle_metrics)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        # aiohttp's access log is noisy on a health-checked host.
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await self._site.start()
        except OSError as exc:  # pragma: no cover - port already in use
            logger.error("health endpoint could not bind %s:%s (%s)", self._host, self._port, exc)
            await self._runner.cleanup()
            self._runner = None
            raise
        logger.info("health endpoint listening on http://%s:%s/healthz", self._host, self._port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    async def _handle_root(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=f"ZIP → Video userbot hosting service v{self._version}\n"
            "Endpoints: /healthz /readyz /metrics\n",
            content_type="text/plain",
        )

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": self._version})

    async def _handle_ready(self, _request: web.Request) -> web.Response:
        mongo_ok = True
        if self._mongo is not None:
            try:
                mongo_ok = bool(await asyncio.wait_for(self._mongo.ping(), timeout=5))
            except Exception:  # pragma: no cover - depends on live server
                mongo_ok = False
        payload = {"status": "ready" if mongo_ok else "degraded", "mongodb": mongo_ok}
        return web.json_response(payload, status=200 if mongo_ok else 503)

    async def _handle_metrics(self, _request: web.Request) -> web.Response:
        payload: dict[str, Any] = {"version": self._version}
        if self._metrics is not None:
            payload["process"] = self._metrics.snapshot()
        if self._userbots is not None:
            payload["userbots"] = self._userbots.snapshot()
        if self._repos is not None:
            try:
                payload["collections"] = {
                    "users": await self._repos.users.total_users(),
                    "sessions": await self._repos.sessions.count(),
                    "tasks_active": await self._repos.tasks.count_active(),
                    "tasks_by_status": await self._repos.tasks.status_breakdown(),
                }
            except Exception as exc:  # pragma: no cover - DB hiccup
                payload["collections"] = {"error": type(exc).__name__}
        return web.json_response(payload)


__all__ = ["HealthServer"]
