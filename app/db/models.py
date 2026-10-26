"""Document models and helpers for MongoDB persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from bson import ObjectId

from app.constants import AccountStatus, StepStatus, TaskStatus, UnzipMode


def utcnow() -> datetime:
    """Naive UTC timestamp (MongoDB stores dates as UTC milliseconds)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if isinstance(value, datetime) else None


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
USERS = "users"
SESSIONS = "sessions"
SESSION_LOCKS = "session_locks"
TASKS = "tasks"
TASK_STEPS = "task_steps"
AUDIT = "audit_log"
HEALTH = "worker_health"


DEFAULT_USER_SETTINGS: Dict[str, Any] = {
    "spoiler_default": False,
    "notify_on_complete": True,
    "keep_files": False,
}


@dataclass
class UserRecord:
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: str = AccountStatus.NEW.value
    is_admin: bool = False
    is_banned: bool = False
    consent_given: bool = False
    consent_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None
    connected_at: Optional[datetime] = None
    disconnected_at: Optional[datetime] = None
    account: Dict[str, Any] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_USER_SETTINGS))
    quota: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "UserRecord":
        data = dict(doc)
        data["user_id"] = int(data.pop("_id"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @property
    def display_name(self) -> str:
        name = " ".join(x for x in (self.first_name, self.last_name) if x).strip()
        return name or (f"@{self.username}" if self.username else str(self.user_id))

    @property
    def is_connected(self) -> bool:
        return self.status == AccountStatus.CONNECTED.value


@dataclass
class TaskRecord:
    task_id: str
    user_id: int
    chat_id: int
    message_id: int
    dedupe_key: str
    mode: str = UnzipMode.PLAIN.value
    status: str = TaskStatus.QUEUED.value
    active: bool = True
    peer: Dict[str, Any] = field(default_factory=dict)
    archive: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    cancel_requested: bool = False
    attempts: int = 0
    worker_id: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "TaskRecord":
        data = dict(doc)
        data["task_id"] = str(data.pop("_id"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @property
    def spoiler(self) -> bool:
        return self.mode == UnzipMode.SPOILER.value

    @property
    def files_total(self) -> int:
        return int(self.stats.get("files_total", 0) or 0)

    @property
    def files_sent(self) -> int:
        return int(self.stats.get("files_sent", 0) or 0)

    def is_active(self) -> bool:
        return self.status in {
            TaskStatus.QUEUED.value,
            TaskStatus.DOWNLOADING.value,
            TaskStatus.EXTRACTING.value,
            TaskStatus.SENDING.value,
        }


@dataclass
class StepRecord:
    task_id: str
    user_id: int
    rel_path: str
    size: int = 0
    status: str = StepStatus.QUEUED.value
    attempts: int = 0
    message_id: Optional[int] = None
    random_id: int = 0
    error: Optional[str] = None
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "StepRecord":
        data = dict(doc)
        data["task_id"] = str(data.get("task_id"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def task_dedupe_key(user_id: int, chat_id: int, message_id: int, mode: str, attempt: int = 0) -> str:
    suffix = f":r{attempt}" if attempt else ""
    return f"{user_id}:{chat_id}:{message_id}:{mode}{suffix}"


def new_object_id() -> ObjectId:
    return ObjectId()


def status_counts_template() -> Dict[str, int]:
    return {status.value: 0 for status in TaskStatus}


def empty_task_stats() -> Dict[str, int]:
    return {
        "files_total": 0,
        "files_sent": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "files_uncertain": 0,
        "bytes_downloaded": 0,
        "bytes_sent": 0,
        "archives_scanned": 0,
    }


def public_task_dict(task: TaskRecord, steps: Optional[List[StepRecord]] = None) -> Dict[str, Any]:
    """A JSON-friendly representation safe to store in the audit log."""
    payload: Dict[str, Any] = {
        "task_id": task.task_id,
        "user_id": task.user_id,
        "chat_id": task.chat_id,
        "message_id": task.message_id,
        "mode": task.mode,
        "status": task.status,
        "stats": dict(task.stats),
        "created_at": iso(task.created_at),
        "finished_at": iso(task.finished_at),
    }
    if task.error:
        payload["error"] = dict(task.error)
    if steps:
        payload["steps"] = [
            {"rel_path": s.rel_path, "status": s.status, "message_id": s.message_id}
            for s in steps
        ]
    return payload
