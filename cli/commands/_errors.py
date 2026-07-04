"""Shared CLI error-rendering helpers (audit F8).

A single home for the *actionable* session-limit message used by both the
one-shot path (``run.py``) and the REPL (``chat.py``).  When ``create_session``
raises the per-user ``AgentError("Session limit reached ...")`` the user almost
always hit a pile of stale interactive sessions from earlier runs — so instead
of echoing the bare exception we print the concrete commands to inspect and
clear them, plus the one-launch env override.
"""

from __future__ import annotations

import click


def is_session_limit_error(exc: BaseException) -> bool:
    """True when *exc* is the per-user session-limit error.

    Matched on the message text (``"Session limit reached"``) rather than the
    exception type so it works regardless of whether the caller imported the
    concrete ``AgentError`` class.
    """
    return "session limit reached" in str(exc).lower()


def session_limit_message(user_id: str = "local") -> str:
    """The actionable session-limit block (no leading ``[polyrob] ERROR:`` styling).

    Returns a plain multi-line string; callers wrap the first line in the red
    ``[polyrob] ERROR:`` style.  Kept as one shared string so ``run.py`` and
    ``chat.py`` stay in lockstep.
    """
    return (
        f"Session limit reached (user '{user_id}').\n"
        "  These are usually stale interactive sessions from earlier runs.\n"
        "  Fix:  polyrob session list          # find stale ids\n"
        "        polyrob session cancel <id>   # cancel them\n"
        "  Or raise the cap for one launch:  MAX_SESSIONS_PER_USER=60 polyrob"
    )


def echo_create_session_error(exc: BaseException, user_id: str = "local") -> None:
    """Echo a ``create_session`` failure with the styled ``[polyrob] ERROR:`` prefix.

    For a session-limit error the actionable F8 block is printed; for anything
    else the raw exception text is echoed.  Used by both ``run.py`` and
    ``chat.py`` so the two surfaces render identically.
    """
    prefix = click.style("[polyrob] ERROR: ", fg="red")
    if is_session_limit_error(exc):
        click.echo(prefix + session_limit_message(user_id))
    else:
        click.echo(prefix + str(exc))
