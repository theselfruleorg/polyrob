"""The ONE import a surface makes to reach the shared inbound dispatch (064 F1).

A surface turns its platform event into an ``InboundMessage``, routes it
(``core.surfaces.dispatcher.route_inbound``) and hands the ``RouteDecision`` to
:func:`act_on_inbound` — the RouteDecision → TaskAgent executor every surface
shares (steer / new session / owner command / correspondent data).

Before 064 F1, email, WhatsApp and the four polled harnesses each imported
``surfaces.telegram.harness`` by name to reach it, so a new surface had to
know that the shared executor happened to live in the Telegram package. They
import THIS module now.

⚠️ The executor's body still lives in ``surfaces/telegram/harness.py``: its
dispatch reaches the Telegram seat's own modules (the admin-verb table, the card
seat, the room and scoped help, the per-chat verbs, the task-session start). What
holds no Telegram module moved below the seats (proposal 125 S14): the command
catalogue (``core/surfaces/command_catalog.py`` — /help, the unknown-verb answer,
the owner-verb set, the room refusals) and the lifecycle gate and `/cancel`
(``core/surfaces/lifecycle_verbs.py``). :func:`help_text` and
:func:`unknown_command_text` read core directly; the executor calls stay a lookup
by ATTRIBUTE at call time, so a test that patches
``surfaces.telegram.harness.act_on_inbound`` still intercepts every surface.
"""
from typing import Any, Optional

from core.surfaces.act import InboundResult  # noqa: F401 — the envelope, re-exported


def _home():
    import surfaces.telegram.harness as home  # registers the core actor at import
    return home


def ensure_registered() -> None:
    """Register the shared executor with ``core.surfaces.act`` (idempotent).
    A webhook surface calls this at import; core delegates through it."""
    _home()


async def act_on_inbound(task_agent: Any, result: InboundResult, **kwargs) -> Optional[str]:
    """Execute one routed inbound — the shared executor, whatever its home."""
    return await _home().act_on_inbound(task_agent, result, **kwargs)


# Proposal 125 S14: the console chat box runs an owner verb through the SAME
# executor as a chat app, and reaches it here — never through the Telegram
# package's private names. Looked up by ATTRIBUTE at call time, like
# ``act_on_inbound``, so a test that patches the home still intercepts.

async def handle_command(task_agent: Any, result: InboundResult, *, deliver=None) -> Any:
    """Run one owner command line (a verb or a tap token) through the executor."""
    return await _home()._handle_command(task_agent, result, spawn=None, deliver=deliver)


def record_owner_line(task_agent: Any, result: InboundResult, *, session_id: str,
                      kind: str) -> None:
    """Record an accepted owner DM line in the ONE owner thread (061). Fail-open."""
    _home()._record_owner_line(task_agent, result, session_id=session_id, kind=kind)


def help_text() -> str:
    """The whole owner verb list, as the chat seats print it for `/help all`."""
    from core.surfaces.command_catalog import help_text as _text
    return _text()


def unknown_command_text(cmd: str) -> str:
    """The answer for a slash line no handler owns (a retired verb, a typo)."""
    from core.surfaces.command_catalog import unknown_text
    return unknown_text(cmd)
