"""Scoped async terminal input seam; providers never import a CLI renderer."""
from contextvars import ContextVar
from contextlib import contextmanager
import asyncio
from typing import Awaitable, Callable, Optional

approval_input: ContextVar[Optional[Callable[[str], Awaitable[str]]]] = ContextVar(
    "approval_input", default=None
)

# Background autonomy tasks may predate the REPL's ContextVar scope. Stdin is
# process-wide, so they must still use its active owner rather than input().
_terminal_owner = None


@contextmanager
def terminal_approval_input(reader):
    global _terminal_owner
    if _terminal_owner is not None:
        raise RuntimeError("A terminal approval input owner is already active")
    owner_loop = asyncio.get_running_loop()

    async def route(prompt):
        if _terminal_owner is not route or owner_loop.is_closed():
            return "deny"
        if asyncio.get_running_loop() is owner_loop:
            return await reader(prompt)
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(reader(prompt), owner_loop))

    _terminal_owner = route
    try:
        yield
    finally:
        _terminal_owner = None


def get_approval_input():
    return approval_input.get() or _terminal_owner
