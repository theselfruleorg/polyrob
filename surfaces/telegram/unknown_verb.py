"""The answer for a slash line that no handler on this seat owns (030 L9, 113 W0).

Three cases, in this order: a verb that was retired (say what replaced it), a REAL
verb that runs on another seat (name the seat — "Unknown command /doctor" was a
confident lie about a verb that exists), and a typo (the closest verbs). Telegram,
the other chat surfaces and the console chat box all answer through here.
"""
from __future__ import annotations

from typing import Iterable

#: The owner words for a seat id (``core.verbs.Verb.seats``).
_SEAT_WORDS = {"repl": "the terminal", "telegram": "Telegram", "console": "the console"}


def elsewhere_text(verb, seat: str) -> str:
    """The line for a real verb this seat cannot run, or "" when it can."""
    if verb is None or verb.runs_on(seat):
        return ""
    where = " or ".join(_SEAT_WORDS.get(s, s) for s in verb.run_seats())
    return (f"{verb.name} — {verb.help}.\nIt runs in {where}, not here. "
            f"Send /help for the verbs this chat has.")


def unknown_command_text(cmd: str, known: Iterable[str], seat: str) -> str:
    import difflib

    from core.surfaces.verb_check import verb_note
    from core.verbs import verb_for
    if cmd == "/admin":
        from surfaces.telegram import room_help
        return room_help.admin_elsewhere(None).text
    if verb_note(cmd):
        return f"{cmd} is gone. {verb_note(cmd)} Send /help for the full list."
    elsewhere = elsewhere_text(verb_for(cmd), seat)
    if elsewhere:
        return elsewhere
    matches = difflib.get_close_matches(cmd, list(known), n=2, cutoff=0.6)
    hint = f" Did you mean {' or '.join(matches)}?" if matches else ""
    return f"Unknown command {cmd}.{hint} Send /help for the full list."
