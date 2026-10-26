"""Structured logging with mandatory secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from typing import Any, Dict, Iterable, Optional

from app.core.redact import Redactor, default_redactor

_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)

# Extra fields that are meaningful for structured logs.
_CONTEXT_KEYS = (
    "user_id",
    "task_id",
    "chat_id",
    "message_id",
    "job",
    "account",
    "step",
    "bytes",
    "duration",
    "status",
    "attempt",
    "component",
)


class RedactingFilter(logging.Filter):
    """Scrubs secrets from the message, the arguments and the traceback."""

    def __init__(self, redactor: Optional[Redactor] = None, name: str = ""):
        super().__init__(name)
        self._redactor = redactor or default_redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = self._redactor.redact(message)
        record.args = ()

        if record.exc_info:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self._redactor.redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)

        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Optional[Redactor] = None):
        super().__init__()
        self._redactor = redactor or default_redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _iso_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = _stringify(value)

        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({k: _stringify(v) for k, v in extra.items()})

        for key, value in record.__dict__.items():
            if key in _RESERVED or key in _CONTEXT_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = _stringify(value)

        if record.exc_text:
            payload["exc"] = record.exc_text
        return self._redactor.redact(json.dumps(payload, ensure_ascii=False, default=str))


class ConsoleFormatter(logging.Formatter):
    def __init__(self, redactor: Optional[Redactor] = None):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
        self._redactor = redactor or default_redactor

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if record.exc_text:
            base = f"{base}\n{record.exc_text}"
        return self._redactor.redact(base)


def _iso_timestamp(created: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{int(created % 1 * 1000):03d}Z"


def _stringify(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Telethon is extremely chatty at INFO; keep it at WARNING unless debugging.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger adapter that always attaches the given context fields."""
    return logging.LoggerAdapter(logging.getLogger(name), context)


def log_extra(**context: Any) -> Dict[str, Any]:
    return dict(context)


def iter_context_keys() -> Iterable[str]:
    return _CONTEXT_KEYS
