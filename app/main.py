"""Application entry point: wire everything together and run it.

Startup order
-------------
1. logging (with mandatory secret redaction)
2. settings (fails fast if a required environment variable is missing)
3. MongoDB connection + index provisioning
4. repositories, session cipher/store, login manager
5. ZIP extractor and the userbot worker manager (restart recovery happens here)
6. aiogram dispatcher and the health endpoint
7. restore workers for accounts that were connected before the restart
8. long polling until SIGTERM/SIGINT, then a graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot import BotServices, build_router
from app.config import Settings, get_settings
from app.core.crypto import SessionCipher
from app.core.metrics import Metrics, metrics as app_metrics
from app.db.mongo import MongoManager
from app.db.repositories import Repositories
from app.health import HealthServer
from app.logging_setup import configure_logging
from app.sessions.manager import SessionManager
from app.sessions.store import SessionStore
from app.userbot.runner import UserbotManager
from app.userbot.telethon_transport import TelethonTransport
from app.zipengine.extractor import build_extractor

logger = logging.getLogger("app.main")

VERSION = "1.0.0"

BOT_COMMANDS = (
    BotCommand(command="start", description="Open your dashboard"),
    BotCommand(command="status", description="Account and quota status"),
    BotCommand(command="jobs", description="Your recent jobs"),
    BotCommand(command="settings", description="Defaults for new jobs"),
    BotCommand(command="cancel", description="Cancel a running job"),
    BotCommand(command="help", description="How this service works"),
)


class Application:
    """Holds every long-lived object so shutdown can be ordered precisely."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.metrics: Metrics = app_metrics
        self.mongo: Optional[MongoManager] = None
        self.repos: Optional[Repositories] = None
        self.sessions: Optional[SessionManager] = None
        self.store: Optional[SessionStore] = None
        self.userbots: Optional[UserbotManager] = None
        self.bot: Optional[Bot] = None
        self.dispatcher: Optional[Dispatcher] = None
        self.health: Optional[HealthServer] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        settings = self.settings

        self.mongo = MongoManager(settings.mongodb_uri, settings.mongodb_db)
        db = await self.mongo.connect()
        self.repos = Repositories.create(
            db,
            default_storage_quota_bytes=settings.default_storage_quota_bytes,
            default_daily_task_quota=settings.default_daily_task_quota,
        )
        # Temporary storage is reserved in memory by each running job. A hard
        # kill leaves the MongoDB counter incremented, so reconcile it before
        # accepting work. This is safe for the single-replica deployment model;
        # multiple replicas would need per-task reservation documents instead.
        cleared = await self.repos.users.reset_all_storage_usage()
        if cleared:
            logger.warning("cleared %d stale storage reservation(s)", cleared)

        cipher = SessionCipher(settings.resolve_session_key())
        self.store = SessionStore(settings.sessions_path, cipher, self.repos)
        await self._prune_orphan_sessions()

        def transport_factory(user_id: int, session_string: str) -> TelethonTransport:
            return TelethonTransport(
                user_id,
                settings.telegram_api_id,
                settings.telegram_api_hash,
                session_string,
                upload_part_size_kb=settings.upload_chunk_size_kb,
            )

        self.sessions = SessionManager(
            settings=settings,
            repos=self.repos,
            store=self.store,
            transport_factory=transport_factory,
            metrics=self.metrics,
        )

        extractor = build_extractor(settings, nested=True)
        self.userbots = UserbotManager(
            settings=settings,
            repos=self.repos,
            sessions=self.sessions,
            extractor=extractor,
            metrics=self.metrics,
        )
        await self.userbots.start()

        self.bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        services = BotServices(
            settings=settings,
            repos=self.repos,
            sessions=self.sessions,
            userbots=self.userbots,
            metrics=self.metrics,
            mongo=self.mongo,
            bot=self.bot,
        )
        self.dispatcher = Dispatcher(storage=MemoryStorage(), services=services)
        self.dispatcher.include_router(build_router())

        self.health = HealthServer(
            host=settings.host,
            port=settings.listen_port,
            mongo=self.mongo,
            metrics=self.metrics,
            userbots=self.userbots,
            repos=self.repos,
            version=VERSION,
        )
        await self.health.start()

        await self._publish_commands()
        await self._restore_connected_accounts()
        logger.info(
            "service ready",
            extra={"component": "startup", "status": "ready", "worker_id": self.userbots.worker_id},
        )

    # ------------------------------------------------------------------
    async def run(self) -> None:
        assert self.bot is not None and self.dispatcher is not None
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, self._stop)

        poller = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                handle_signals=False,
                close_bot_session=True,
            ),
            name="aiogram-polling",
        )
        stop_waiter = asyncio.create_task(self._stop.wait(), name="shutdown-waiter")
        try:
            done, _ = await asyncio.wait(
                {poller, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if poller in done:
                # Polling stopped on its own (bad token, conflict, fatal API
                # error). Surface the failure instead of hanging forever.
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
                exc = poller.exception()
                if exc is not None:
                    raise exc
                logger.error("aiogram polling stopped unexpectedly")
        finally:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
            logger.info("shutdown requested", extra={"component": "shutdown"})
            await self.dispatcher.stop_polling()
            try:
                await asyncio.wait_for(poller, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                poller.cancel()

    async def shutdown(self) -> None:
        if self.health is not None:
            try:
                await self.health.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("health shutdown failed: %s", type(exc).__name__)
        if self.userbots is not None:
            try:
                await self.userbots.shutdown()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("userbot shutdown failed: %s", type(exc).__name__)
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:  # pragma: no cover - best effort
                pass
        if self.mongo is not None:
            try:
                await self.mongo.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("MongoDB shutdown failed: %s", type(exc).__name__)
        logger.info("shutdown complete", extra={"component": "shutdown", "status": "stopped"})

    # ------------------------------------------------------------------
    async def _publish_commands(self) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.set_my_commands(list(BOT_COMMANDS))
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("could not publish the bot command list: %s", type(exc).__name__)

    async def _restore_connected_accounts(self) -> None:
        """Bring back workers for accounts that were connected before a restart.

        This is bounded by ``MAX_ACTIVE_USERBOTS``; any account that does not fit
        is simply started on demand the next time it is used (it is never
        reported as online when it is not).
        """
        assert self.repos is not None and self.userbots is not None
        try:
            connected = await self.repos.users.list_connected_ids()
        except Exception as exc:  # pragma: no cover - DB hiccup
            logger.warning("could not list connected accounts: %s", type(exc).__name__)
            return
        restored = 0
        for user_id in connected[: int(self.settings.max_active_userbots)]:
            worker = await self.userbots.ensure(user_id)
            if worker is not None:
                restored += 1
        if connected:
            logger.info(
                "restored %d of %d connected account(s)", restored, len(connected),
                extra={"component": "startup", "status": "restored"},
            )

    async def _prune_orphan_sessions(self) -> None:
        """Remove local session directories whose owner no longer exists."""
        if self.store is None or self.repos is None:
            return
        local = {int(uid) for uid in self.store.list_local_sessions()}
        if not local:
            return
        keep: set[int] = set()
        for user_id in sorted(local):
            if await self.repos.users.get(user_id) is not None:
                keep.add(user_id)
        removed = self.store.prune_unknown(keep)
        if removed:
            logger.warning("removed %d orphaned local session(s)", len(removed))


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):  # pragma: no cover - Windows
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------------------
async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    logger.info("starting service v%s", VERSION, extra={"component": "startup"})
    logger.info("configuration: %s", settings.describe(), extra={"component": "startup"})

    app = Application(settings)
    try:
        await app.start()
        await app.run()
    finally:
        await app.shutdown()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    run()
