"""Shared services handed to every aiogram handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from aiogram import Bot

from app.core.metrics import Metrics
from app.db.repositories import Repositories
from app.sessions.manager import SessionManager
from app.userbot.runner import UserbotManager


@dataclass
class BotServices:
    settings: Any
    repos: Repositories
    sessions: SessionManager
    userbots: UserbotManager
    metrics: Metrics
    mongo: Optional[Any] = None
    bot: Optional[Bot] = None
    extras: dict = field(default_factory=dict)

    # -- helpers ---------------------------------------------------------
    def is_admin(self, user_id: Optional[int]) -> bool:
        return self.settings.is_admin(user_id)

    async def register_user(self, from_user) -> Any:
        """Upsert the Telegram user and return the stored record."""
        return await self.repos.users.ensure_user(
            int(from_user.id),
            username=getattr(from_user, "username", None),
            first_name=getattr(from_user, "first_name", None),
            last_name=getattr(from_user, "last_name", None),
            is_admin=self.is_admin(from_user.id),
        )

    async def notify(self, user_id: int, text: str) -> bool:
        """Send a control-bot message to a user (best effort)."""
        if self.bot is None:
            return False
        try:
            await self.bot.send_message(int(user_id), text)
            return True
        except Exception:  # pragma: no cover - depends on Telegram limits
            return False


__all__ = ["BotServices"]
