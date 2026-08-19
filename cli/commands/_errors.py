"""Shared CLI error-rendering helpers (audit F8).

A single home for the *actionable* session-limit message used by both the
one-shot path (``run.py``) and the REPL (``chat.py``).  When ``create_session``
raises the per-user ``AgentError("Session limit reached ...")`` the user almost
always hit a pile of stale interactive sessions from earlier runs — so instead
of echoing the bare exception we print the concrete commands to inspect and
clear them, plus the one-launch env override.
"""

from __future__ import annotations

import re
from typing import Optional

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


# Sentinel prefixes task_agent_lite returns for a run that did NOT succeed
# (kept lowercase; see 027 WP3 — a failed one-shot run must exit non-zero).
_FAILURE_SENTINELS = (
    "session failed",
    "task package not available",
    "no active session",
    "session not found",
    "session cancelled",
)


def session_exit_code(done_success: Optional[bool], result_text: str) -> int:
    """Exit code for a finished one-shot run.

    ``done_success`` is the SessionDone feed event's flag when one arrived
    (authoritative); with no event we fall back to the sentinel strings
    ``run_session`` returns on failure paths.
    """
    if done_success is False:
        return 1
    if done_success is True:
        return 0
    text = (result_text or "").strip().lower()
    return 1 if text.startswith(_FAILURE_SENTINELS) else 0


def remedy_line(error_text: str) -> Optional[str]:
    """One actionable next step for a known failure class, or None.

    Keyed on message text so it works at the CLI boundary where only the
    rendered error string survives (the typed exception died in the agent
    loop). Kept aligned with core/error_classifier.py classes.
    """
    text = error_text or ""
    lowered = text.lower()

    # Missing optional dependency → pip extra remedy (one SSOT).
    from core.optional_extras import missing_extra_hint

    hint = missing_extra_hint(text)
    if hint:
        return f"fix: {hint}"

    if (
        "authenticationerror" in lowered
        or "401" in text
        or "unauthorized" in lowered
        or "invalid api key" in lowered
    ):
        match = re.search(r"from ([A-Za-z][A-Za-z0-9_-]+)", text)
        provider = match.group(1).lower() if match else "<provider>"
        return (
            f"fix: the provider rejected your key — reconnect with "
            f"`polyrob auth add {provider}` (keys live in ~/.polyrob/.env)"
        )

    if (
        "insufficient_quota" in lowered
        or "insufficient credit" in lowered
        or "402" in text
        or "exceeded your current quota" in lowered
    ):
        return (
            "fix: the provider account is out of credit — top up its billing, "
            "or connect another provider with `polyrob auth add <provider>`"
        )

    return None


def require_extra_or_exit(extra: str, modules=None) -> None:
    """Preflight an optional extra BEFORE any side-effecting startup.

    Surface commands used to crash with a raw ModuleNotFoundError after the
    container build (and dashboard after opening a browser tab). One line +
    exit 1 instead.
    """
    from core.optional_extras import require_extra

    try:
        require_extra(extra, modules=modules)
    except ImportError as exc:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + str(exc), err=True)
        raise SystemExit(1)


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
