"""Centralised, rate-limit-aware request scheduling.

Every Telegram call made *through a user account* goes through
:class:`AccountScheduler`. It:

* serialises requests per account up to a configurable concurrency,
* honours ``FloodWaitError`` by pausing the whole account for the exact wait,
* retries transient/network failures with exponential backoff + jitter,
* never retries permanent errors (permission, invalid request, revoked session),
* and refuses to blindly retry operations that may have already been delivered.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar

from app.core.metrics import Metrics, metrics as default_metrics
from app.core.retry import backoff_delay
from app.userbot.transport import (
    FloodWaitSignal,
    PermanentTransportError,
    TransientTransportError,
    UncertainSendError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class SchedulerStats:
    requests: int = 0
    retries: int = 0
    flood_waits: int = 0
    flood_wait_seconds: float = 0.0
    permanent_errors: int = 0
    uncertain_errors: int = 0
    total_wait_seconds: float = 0.0
    per_operation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "flood_waits": self.flood_waits,
            "flood_wait_seconds": round(self.flood_wait_seconds, 2),
            "permanent_errors": self.permanent_errors,
            "uncertain_errors": self.uncertain_errors,
            "total_wait_seconds": round(self.total_wait_seconds, 2),
            "per_operation": dict(self.per_operation),
        }


class AccountScheduler:
    def __init__(
        self,
        user_id: int,
        *,
        max_concurrency: int = 2,
        min_interval: float = 0.0,
        max_retries: int = 4,
        base_delay: float = 2.0,
        max_delay: float = 120.0,
        flood_extra: float = 1.0,
        uncertain_retries: int = 0,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self.user_id = int(user_id)
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_interval = max(0.0, float(min_interval))
        self.max_retries = max(0, int(max_retries))
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.flood_extra = max(0.0, float(flood_extra))
        self.uncertain_retries = max(0, int(uncertain_retries))
        self.stats = SchedulerStats()

        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._rand = rand
        self._metrics = metrics or default_metrics
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._gate_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._flood_until = 0.0

    # -- public API -------------------------------------------------------
    @property
    def paused_for(self) -> float:
        """Seconds until this account may talk to Telegram again."""
        return max(0.0, self._flood_until - self._clock())

    def note_flood_wait(self, seconds: float) -> None:
        deadline = self._clock() + max(0.0, float(seconds)) + self.flood_extra
        self._flood_until = max(self._flood_until, deadline)

    async def execute(
        self,
        operation: str,
        factory: Callable[[], Awaitable[T]],
        *,
        retry_transient: bool = True,
        idempotent: bool = True,
    ) -> T:
        """Run ``factory`` under account-level rate limiting.

        ``idempotent=False`` means the operation may already have been applied
        when a transient failure occurs (e.g. sending a plain text message).
        Such operations are not retried automatically. Uploads and sends that
        carry a deterministic ``random_id`` are safe to retry and use
        ``idempotent=True``.
        """
        attempt = 0
        while True:
            attempt += 1
            await self._wait_for_gate()
            async with self._semaphore:
                await self._acquire_slot()
                try:
                    result = await factory()
                except FloodWaitSignal as exc:
                    self.stats.flood_waits += 1
                    self.stats.flood_wait_seconds += exc.seconds
                    self._metrics.incr("scheduler.flood_wait")
                    self.note_flood_wait(exc.seconds)
                    logger.warning(
                        "flood wait for account %s: %.0fs (operation=%s, attempt=%d)",
                        self.user_id,
                        exc.seconds,
                        operation,
                        attempt,
                    )
                    if attempt > self.max_retries:
                        raise
                    continue
                except UncertainSendError:
                    self.stats.uncertain_errors += 1
                    self._metrics.incr("scheduler.uncertain")
                    if idempotent and attempt <= self.uncertain_retries:
                        await self._backoff(attempt, operation, "uncertain-send")
                        continue
                    raise
                except TransientTransportError as exc:
                    self._metrics.incr("scheduler.transient_error")
                    if retry_transient and idempotent and attempt <= self.max_retries:
                        await self._backoff(attempt, operation, type(exc).__name__)
                        continue
                    raise
                except PermanentTransportError:
                    self.stats.permanent_errors += 1
                    self._metrics.incr("scheduler.permanent_error")
                    raise
                except asyncio.CancelledError:
                    raise
                else:
                    self.stats.requests += 1
                    self.stats.per_operation[operation] = (
                        self.stats.per_operation.get(operation, 0) + 1
                    )
                    self._metrics.incr(f"scheduler.request.{operation}")
                    return result

    # -- internals --------------------------------------------------------
    async def _wait_for_gate(self) -> None:
        remaining = self.paused_for
        if remaining > 0:
            self.stats.total_wait_seconds += remaining
            await self._sleep(remaining)

    async def _acquire_slot(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._gate_lock:
            now = self._clock()
            if self._next_slot > now:
                delay = self._next_slot - now
                self.stats.total_wait_seconds += delay
                await self._sleep(delay)
                now = self._clock()
            self._next_slot = now + self.min_interval

    async def _backoff(self, attempt: int, operation: str, reason: str) -> None:
        delay = backoff_delay(
            attempt, base=self.base_delay, cap=self.max_delay, rand=self._rand
        )
        self.stats.retries += 1
        self.stats.total_wait_seconds += delay
        self._metrics.incr("scheduler.retry")
        logger.info(
            "retrying account %s operation %s in %.1fs (attempt %d, reason=%s)",
            self.user_id,
            operation,
            delay,
            attempt,
            reason,
        )
        await self._sleep(delay)

    def as_dict(self) -> dict:
        return {"user_id": self.user_id, "paused_for": round(self.paused_for, 2), **self.stats.as_dict()}
