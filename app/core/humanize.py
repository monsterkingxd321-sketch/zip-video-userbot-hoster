"""Human readable formatting shared by the bot UI and the task pipeline."""

from __future__ import annotations

from typing import Optional

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(value: Optional[float], *, precision: int = 1) -> str:
    """``1536`` -> ``1.5 KB``."""
    if value is None:
        return "?"
    amount = float(value)
    if amount < 0:
        amount = 0.0
    index = 0
    while amount >= 1024 and index < len(_UNITS) - 1:
        amount /= 1024.0
        index += 1
    if index == 0:
        return f"{int(amount)} {_UNITS[index]}"
    return f"{amount:.{precision}f} {_UNITS[index]}"


def format_duration(seconds: Optional[float]) -> str:
    """``3725`` -> ``1h 2m``. Returns ``""`` for unknown values."""
    if seconds is None:
        return ""
    total = int(max(0.0, float(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def format_speed(bytes_per_second: Optional[float]) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return f"{format_bytes(bytes_per_second)}/s"


def format_percent(done: float, total: float, *, precision: int = 0) -> str:
    if not total or total <= 0:
        return ""
    ratio = max(0.0, min(1.0, float(done) / float(total)))
    return f"{ratio * 100:.{precision}f}%"


def progress_bar(done: float, total: float, *, width: int = 12) -> str:
    if not total or total <= 0:
        return ""
    ratio = max(0.0, min(1.0, float(done) / float(total)))
    filled = int(round(ratio * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def truncate(text: str, limit: int = 64) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    keep = max(1, limit - 1)
    return text[:keep] + "…"


def humanize_transfer(
    received: int,
    total: Optional[int],
    *,
    started_at: float,
    now: float,
) -> str:
    """A one-line progress string with size, percent, speed and ETA."""
    elapsed = max(0.0, now - started_at)
    parts = [f"{format_bytes(received)}"]
    if total:
        parts[0] = f"{format_bytes(received)}/{format_bytes(total)}"
        percent = format_percent(received, total)
        if percent:
            parts.append(percent)
    if elapsed > 0.5:
        speed = received / elapsed
        parts.append(format_speed(speed))
        if total and speed > 0:
            remaining = max(0.0, (total - received) / speed)
            eta = format_duration(remaining)
            if eta:
                parts.append(f"ETA {eta}")
    return " · ".join(part for part in parts if part)
