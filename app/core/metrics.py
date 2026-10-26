"""Lightweight in-process metrics and counters exposed by the health endpoint."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping, MutableMapping


class Metrics:
    """Thread-safe-enough (single event loop) counters + gauges + histograms."""

    def __init__(self) -> None:
        self._counters: MutableMapping[str, float] = defaultdict(float)
        self._gauges: MutableMapping[str, float] = {}
        self._timings: MutableMapping[str, list[float]] = defaultdict(list)
        self._started_at = time.time()

    # -- counters ---------------------------------------------------------
    def incr(self, name: str, value: float = 1.0) -> None:
        self._counters[name] += value

    def counter(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    # -- gauges -----------------------------------------------------------
    def gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def adjust_gauge(self, name: str, delta: float) -> None:
        self._gauges[name] = self._gauges.get(name, 0.0) + delta

    # -- timings ----------------------------------------------------------
    def observe(self, name: str, seconds: float, *, keep: int = 200) -> None:
        bucket = self._timings[name]
        bucket.append(float(seconds))
        if len(bucket) > keep:
            del bucket[: len(bucket) - keep]

    def timing_summary(self, name: str) -> Dict[str, float]:
        values = sorted(self._timings.get(name, ()))
        if not values:
            return {"count": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        count = len(values)

        def pick(pct: float) -> float:
            idx = min(count - 1, max(0, int(round(pct * (count - 1)))))
            return values[idx]

        return {
            "count": float(count),
            "avg": sum(values) / count,
            "p50": pick(0.5),
            "p95": pick(0.95),
            "max": values[-1],
        }

    # -- export -----------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "counters": {k: round(v, 3) for k, v in sorted(self._counters.items())},
            "gauges": {k: round(v, 3) for k, v in sorted(self._gauges.items())},
            "timings": {k: self.timing_summary(k) for k in sorted(self._timings)},
        }

    def update_gauges(self, values: Mapping[str, float]) -> None:
        for key, value in values.items():
            self.gauge(key, value)

    def keys(self) -> Iterable[str]:
        return self._counters.keys()


metrics = Metrics()
