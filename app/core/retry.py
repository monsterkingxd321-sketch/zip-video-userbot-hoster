"""Backoff helpers shared by the scheduler and the task pipeline."""

from __future__ import annotations

import random
from typing import Callable


def backoff_delay(
    attempt: int,
    *,
    base: float = 2.0,
    cap: float = 120.0,
    jitter_ratio: float = 0.25,
    rand: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with full jitter.

    ``attempt`` is 1-based: attempt 1 -> roughly ``base`` seconds.
    """
    if attempt < 1:
        attempt = 1
    raw = min(cap, base * (2 ** (attempt - 1)))
    if jitter_ratio <= 0:
        return raw
    jitter = raw * jitter_ratio
    return max(0.0, raw - jitter + (2 * jitter * rand()))


def stable_int64(*parts: object) -> int:
    """Deterministic signed 64-bit id derived from the given parts.

    Used as the MTProto ``random_id`` so that retrying an identical send is
    idempotent on Telegram's side (Telegram de-duplicates by random_id).
    """
    import hashlib

    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False) & 0x7FFFFFFFFFFFFFFF
    return value or 1
