"""MongoDB connection management and index provisioning."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import PyMongoError

from app.db import models as M

logger = logging.getLogger(__name__)


def default_client_factory(uri: str, **kwargs: Any) -> AsyncIOMotorClient:
    return AsyncIOMotorClient(uri, **kwargs)


class MongoManager:
    """Owns the Motor client and creates the indexes the platform relies on."""

    def __init__(
        self,
        uri: str,
        db_name: str,
        *,
        client_factory: Callable[..., Any] = default_client_factory,
        server_selection_timeout_ms: int = 8000,
        **client_kwargs: Any,
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self._client_factory = client_factory
        self._client_kwargs = client_kwargs
        self._timeout_ms = server_selection_timeout_ms
        self._client: Optional[Any] = None
        self._db: Optional[Any] = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("MongoDB client is not connected yet")
        return self._client

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("MongoDB database is not connected yet")
        return self._db

    async def connect(self) -> Any:
        if self._client is not None:
            return self._db
        kwargs = dict(self._client_kwargs)
        kwargs.setdefault("serverSelectionTimeoutMS", self._timeout_ms)
        kwargs.setdefault("retryWrites", True)
        kwargs.setdefault("appname", "zip-userbot-hoster")
        try:
            self._client = self._client_factory(self._uri, **kwargs)
        except TypeError:
            # Some fakes (e.g. mongomock) do not accept every kwarg.
            self._client = self._client_factory(self._uri)
        self._db = self._client[self._db_name]
        await self._db.command("ping")
        await self.ensure_indexes()
        return self._db

    async def ping(self) -> bool:
        try:
            await self.db.command("ping")
            return True
        except Exception:  # pragma: no cover - depends on live server
            return False

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None

    # ------------------------------------------------------------------
    async def ensure_indexes(self) -> None:
        await self._safe_create(
            M.USERS,
            [
                IndexModel([("status", ASCENDING)]),
                IndexModel([("updated_at", DESCENDING)]),
                IndexModel([("usage.day", ASCENDING)]),
            ],
        )
        await self._safe_create(
            M.SESSIONS,
            [
                IndexModel([("updated_at", DESCENDING)]),
                IndexModel([("tg_user_id", ASCENDING)]),
            ],
        )
        await self._safe_create(
            M.SESSION_LOCKS,
            [
                IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
                IndexModel([("worker_id", ASCENDING)]),
            ],
        )
        await self._safe_create(
            M.TASKS,
            [
                # A user cannot have two *active* jobs for the same message+mode.
                IndexModel(
                    [("dedupe_key", ASCENDING)],
                    unique=True,
                    name="uniq_active_dedupe",
                    partialFilterExpression={"active": True},
                ),
                IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
                IndexModel([("status", ASCENDING), ("heartbeat_at", ASCENDING)]),
                IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
                IndexModel([("created_at", DESCENDING)]),
            ],
        )
        await self._safe_create(
            M.TASK_STEPS,
            [
                IndexModel(
                    [("task_id", ASCENDING), ("rel_path", ASCENDING)],
                    unique=True,
                    name="uniq_task_step",
                ),
                IndexModel([("task_id", ASCENDING), ("status", ASCENDING)]),
                IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
            ],
        )
        await self._safe_create(
            M.AUDIT,
            [
                IndexModel([("ts", DESCENDING)]),
                IndexModel([("target_user_id", ASCENDING), ("ts", DESCENDING)]),
            ],
        )
        await self._safe_create(
            M.HEALTH,
            [IndexModel([("updated_at", ASCENDING)], expireAfterSeconds=86400)],
        )

    async def _safe_create(self, collection: str, indexes: list[IndexModel]) -> None:
        try:
            await self.db[collection].create_indexes(indexes)
        except PyMongoError as exc:  # pragma: no cover - depends on server
            # Partial/unique indexes may be unsupported on some deployments
            # (e.g. mongomock in tests). Log loudly and continue.
            logger.warning("Could not create indexes on %s: %s", collection, exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Index creation failed on %s: %s", collection, exc)
