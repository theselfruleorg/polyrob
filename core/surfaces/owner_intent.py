"""Owner pause vocabulary for the SLASH verbs (``/pause``, ``/resume``,
``polyrob autonomy pause``). Pure: no I/O, no LLM.

⚠️ Plain chat text is NEVER parsed as a command here or anywhere else. Until
2026-09-24 this module also held ``owner_stop_intent``: a pre-LLM gate that took
any message whose first word was stop/halt/pause/freeze/standby/resume/unpause/
unfreeze and acted on it without the agent. It intercepted ordinary sentences
("resume where you left off", "pause the video", "stop worrying about the
ledger") and answered "Ok please resume buyback" by pausing EVERYTHING. The
owner's rule: a command is a slash command (or a button); a sentence is chat and
goes to the agent, which has the owner-only ``autonomy_control`` action. The
deterministic, model-free stop is ``/pause`` (``/halt``) — it needs no model,
no queue and no tool path. ``tests/unit/core/surfaces/test_no_plain_word_commands.py``
pins the rule.
"""
import re
from typing import Dict, Optional, Tuple

from core.autonomy_control import SCOPES

_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}
_DUR_RE = re.compile(r"^(\d+)([mhd])$")

#: A19 — the owner rejected the raw scope tokens as vocabulary ("trading
#: streams planner cron social oversight pings apps"); every seat now also
#: takes five plain words. ``oversight`` is dormant (A8/A42) and deliberately
#: gets no plain word. Order here is the order the refusal message lists them
#: in, so keep it insertion-ordered.
PLAIN_WORDS: Dict[str, Tuple[str, ...]] = {
    "everything": ("all",),
    "trading": ("trading",),
    "background": ("streams", "planner", "cron"),
    "messages": ("pings",),
    "deploying": ("apps",),
}


def parse_pause_args(args: list) -> Tuple[Tuple[str, ...], Optional[int]]:
    """``/pause [word…] [for <N>m|h|d]`` → ``(scopes, duration_minutes)``.

    A word is either a plain word (:data:`PLAIN_WORDS`, expanded to its
    scopes) or a raw scope token (``core.autonomy_control.SCOPES`` — kept
    working for existing callers/tests). Shared by Telegram ``/pause``, the
    REPL ``/pause`` and ``polyrob autonomy pause``. Raises ``ValueError`` with
    an owner-readable message on an unknown word or a bad duration.
    """
    scopes: list = []
    duration: Optional[int] = None
    it = iter(list(args or []))
    for tok in it:
        t = str(tok).lower()
        if t == "for":
            spec = str(next(it, ""))
            m = _DUR_RE.match(spec.lower())
            if not m:
                raise ValueError(f"bad duration {spec!r} (use e.g. 90m, 6h, 2d)")
            n, unit = int(m.group(1)), m.group(2)
            duration = n * _UNIT_MINUTES[unit]
            if duration <= 0:
                raise ValueError("duration must be at least 1 minute")
        elif t in PLAIN_WORDS:
            scopes.extend(PLAIN_WORDS[t])
        elif t in SCOPES:
            scopes.append(t)
        else:
            raise ValueError(
                f"unknown scope {tok!r} — use one of: {', '.join(PLAIN_WORDS)} "
                f"(or a scope: {', '.join(SCOPES)})")
    return (tuple(dict.fromkeys(scopes)) or ("all",)), duration
