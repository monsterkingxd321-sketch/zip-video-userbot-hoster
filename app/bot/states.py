"""FSM states for the account connection flow.

Only a state marker is kept (in aiogram's in-memory storage). No login code and
no password is ever stored in the FSM: they are handled inside the coroutine
that received them and immediately discarded.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ConnectFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()


__all__ = ["ConnectFlow"]
