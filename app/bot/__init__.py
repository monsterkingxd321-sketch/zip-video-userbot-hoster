"""Control bot: routers, texts and shared services."""

from __future__ import annotations

from aiogram import Router

from app.bot.admin import router as admin_router
from app.bot.connect import router as connect_router
from app.bot.dashboard import router as dashboard_router
from app.bot.services import BotServices


def build_router() -> Router:
    """Aggregate every feature router into one root router.

    Order matters: the dashboard owns the general commands, then the operator
    router, then the stateful connect flow (its handlers are state filtered, so
    they can never hijack a normal command).
    """
    root = Router(name="root")
    root.include_router(dashboard_router)
    root.include_router(admin_router)
    root.include_router(connect_router)
    return root


__all__ = ["BotServices", "build_router"]
