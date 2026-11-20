"""The ONE map from the surface a line arrived on to the seat that names it.

Two names of the same fact had two maps: the audit ``via`` of an owner line
(``surfaces/telegram/status_ops._seat_via``: the surface id, ``webview`` read as
``console``) and the turn seat a call card runs as
(``tools/controller/call_card_run.seat_of``: a ``core.security.turn_facts.SEATS``
member, ``owner`` for any other chat app). Slack and Discord read ``slack`` in one and
``owner`` in the other. Both now read this module.
"""
from typing import Any

#: A surface id whose seat has another name.
_ALIASES = {"webview": "console", "cli": "repl"}


def seat_via(surface_id: Any, default: str = "telegram") -> str:
    """The seat a line ACTUALLY arrived on, for an audit ``via``: the surface id
    (``slack``, ``discord`` …), the console for ``webview``, the REPL for ``cli``;
    ``default`` when the surface is unknown."""
    sid = str(surface_id or "").strip().lower()
    if not sid:
        return default
    return _ALIASES.get(sid, sid)


def turn_seat(surface_id: Any) -> str:
    """The owner-seat turn kind (``core.security.turn_facts.SEATS``) a verb from this
    surface runs as: its own seat when it has one, else ``owner`` (any other chat app)."""
    from core.security.turn_facts import SEATS
    via = seat_via(surface_id, default="")
    return via if via in SEATS else "owner"


__all__ = ["seat_via", "turn_seat"]
