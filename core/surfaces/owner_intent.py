"""031 — deterministic owner stop/resume intent. Pure: no I/O, no LLM.

Why this exists: four of the owner's six stop messages in the 2026-09-02 week
were queued as "comments" into a session that was mid-run and acted on at its
next step, minutes later; the HITL queue drops a message when full; a
credit-dead provider kills the tool path. A stop must not depend on a model
call. This parser is the only path that has to work when everything else is
down, so it depends on nothing.

Contract (see tests/unit/core/surfaces/test_owner_intent.py):

* ``full_stop`` — the message is a stop-word with, at most, object-free words
  around it ("stop", "stop everything", "stop all ghosts today please",
  "autonomy off", "dude stop it"). The surface applies a pause of EVERYTHING
  without a model call.
* ``resume`` — the same shape with a resume-word ("resume", "unpause"; a bare
  "continue" is chat — it lifts a pause only as "continue autonomy" / "continue
  everything"). The surface lifts the pause when one is on.
* A multi-clause message ("Stop\nwhat's the balance?") is NOT taken here (the
  question mark sends it to the agent, which calls ``autonomy_control``); only a
  pure stop/resume message takes the deterministic path.
* ``scoped`` — a stop/resume-word followed by a real object ("stop trading",
  "stop rendering endless videos", "resume trading"). Passed to the agent,
  which narrows it through the ``autonomy_control`` action — unless the model
  path is known-dead, in which case the surface pauses everything.
* ``None`` — not a stop at all: a negation or question ("don't stop the exit
  monitor", "why did you stop?"), or the stop-word is not the verb ("the stop
  loss is wrong").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from core.autonomy_control import SCOPES

VOICE_TRANSCRIPT_PREFIX = "[voice message, auto-transcribed] "

STOP_WORDS = ("stop", "halt", "pause", "freeze", "standby")
RESUME_WORDS = ("resume", "unpause", "unfreeze")
#: "continue" is the everyday "keep going with your answer" word; it lifts a
#: pause only with an autonomy word next to it ("continue autonomy").
CONTINUE_WORD = "continue"
AUTONOMY_WORDS = frozenset({"autonomy", "autonomous", "everything", "all", "goals", "goal",
                            "loops", "loop", "work", "runs", "jobs", "streams", "trading"})
#: Multi-word forms that mean a FULL stop on their own. Matched as phrases and
#: removed from the text before the per-word check.
FULL_PHRASES = ("autonomy off", "autonomy cancel", "kill switch", "stop everything",
                "stop it all", "stop all", "stop the agent", "shut it down", "stand down")
NEGATIONS = ("don't", "dont", "do not", "never", "not", "except", "only", "but", "why",
             "when", "did", "didn't", "haven't", "havent", "have")
#: Interjections that may PRECEDE the verb without changing the intent
#: ("ok stop", "dude stop it"). Deliberately NOT "the"/"a": "the stop loss is
#: wrong" must not parse as a stop.
LEADING_FILLER = frozenset({
    "ok", "okay", "dude", "bro", "rob", "please", "yes", "yeah", "hey", "haha",
    "baby", "right", "now", "and", "so", "just", "hi",
})
#: Words that may FOLLOW a stop-word WITHOUT making the intent scoped.
OBJECT_FREE = frozenset({
    "all", "everything", "now", "please", "today", "tonight", "for", "the", "a", "an",
    "agent", "autonomy", "autonomous", "work", "working", "goals", "goal", "runs", "jobs",
    "cancel", "it", "this", "that", "dude", "bro", "rob", "right", "immediately", "ghosts",
    "ok", "okay", "and", "your", "yourself", "yes", "off", "everyone", "anything",
    "activity", "activities", "completely", "entirely", "totally", "fully",
})
MAX_WORDS = 12

_WORD_RE = re.compile(r"[a-z0-9']+")
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}
_DUR_PROSE_RE = re.compile(
    r"\bfor\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\b")


@dataclass(frozen=True)
class Intent:
    kind: str                      # "full_stop" | "resume" | "scoped"
    duration_minutes: Optional[int]  # from prose ("for 6 hours"); None = indefinite
    raw: str                       # the owner's words, voice prefix stripped


def strip_voice_prefix(text: str) -> str:
    t = text or ""
    return t[len(VOICE_TRANSCRIPT_PREFIX):] if t.startswith(VOICE_TRANSCRIPT_PREFIX) else t


def _words(text: str) -> list:
    return _WORD_RE.findall(text.lower())


def _prose_duration(low: str) -> Tuple[str, Optional[int]]:
    """Pull a trailing 'for 6 hours' / 'for 90 min' out of the text."""
    m = _DUR_PROSE_RE.search(low)
    if not m:
        return low, None
    n, unit = int(m.group(1)), m.group(2)
    mult = _UNIT_MINUTES[unit[0]]
    minutes = n * mult
    return (low[:m.start()] + " " + low[m.end():]), (minutes if minutes > 0 else None)


def owner_stop_intent(text: str, *, extra_phrases: Tuple[str, ...] = ()) -> Optional[Intent]:
    raw = strip_voice_prefix(text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if "?" in raw:
        return None
    low, duration = _prose_duration(low)
    if any(n in _words(low) for n in NEGATIONS):
        return None
    # a full phrase ("kill switch", "shut it down") counts as one stop word
    phrase_hit = False
    for phrase in FULL_PHRASES:
        if phrase in low:
            low = low.replace(phrase, " stop ")
            phrase_hit = True
    words = _words(low)
    if not words:
        return None
    object_free = OBJECT_FREE | {str(p).lower() for p in extra_phrases}
    idx = 0
    while idx < len(words) - 1 and words[idx] in LEADING_FILLER:
        idx += 1
    first = words[idx]
    rest = words[idx + 1:]
    short = len(words) <= MAX_WORDS
    if first in RESUME_WORDS:
        if short and all(w in object_free for w in rest):
            return Intent("resume", None, raw)
        return Intent("scoped", duration, raw)
    if first == CONTINUE_WORD:
        if short and any(w in AUTONOMY_WORDS for w in rest) and all(w in object_free for w in rest):
            return Intent("resume", None, raw)
        return None  # plain "continue" is chat
    if first in STOP_WORDS:
        if short and all(w in object_free or w in STOP_WORDS for w in rest):
            return Intent("full_stop", duration, raw)
        return Intent("scoped", duration, raw)
    if phrase_hit and short and all(w in object_free or w in STOP_WORDS for w in words):
        return Intent("full_stop", duration, raw)
    return None


_DUR_RE = re.compile(r"^(\d+)([mhd])$")


def parse_pause_args(args: list) -> Tuple[Tuple[str, ...], Optional[int]]:
    """``/pause [scope…] [for <N>m|h|d]`` → ``(scopes, duration_minutes)``.

    Shared by Telegram ``/pause``, the REPL ``/pause`` and
    ``polyrob autonomy pause``. Raises ``ValueError`` with an owner-readable
    message on an unknown scope or a bad duration.
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
        elif t in SCOPES:
            scopes.append(t)
        else:
            raise ValueError(f"unknown scope {tok!r} (one of {', '.join(SCOPES)})")
    return (tuple(dict.fromkeys(scopes)) or ("all",)), duration
