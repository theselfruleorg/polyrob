"""The one-token command grammar — what the owner can decide with a single tap.

A chat client auto-links a ``/word`` token made of ``[A-Za-z0-9_]`` and sends
the WHOLE token when it is tapped. It does NOT link a trailing argument. So
``/approve tap-abc`` rendered only ``/approve`` as tappable and the owner had to
copy the id by hand off a phone (his words, 2026-09-12: "the whole command
should be highlighted so i could tap on it").

The answer is to fold the argument INTO the token: ``/approve_tap_<hex>``,
``/approve_p_<hex>``, ``/approve_all``. Underscores map back to hyphens, which
is only safe because every accepted suffix is separator-free by construction —
a skill or correspondent id is not, so guessing which ``_`` was a ``-`` would
act on the wrong item, and that shape is refused rather than mis-targeted.

This lives in ``core`` because two different tiers need the SAME grammar and
neither may own it alone: the Telegram harness PARSES a tapped token, and
``core.owner_remedy`` must RECOGNISE one so its invented-verb checker does not
flag the framework's own tappable remedies. One grammar, one file.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

#: The verbs that carry a folded argument. Each must also be a real chat verb
#: (``core.surfaces.dispatcher._COMMANDS``) — the token is that verb plus an
#: argument, never a verb of its own.
TAPPABLE_VERBS: Tuple[str, ...] = ("/approve", "/reject")

#: Accepted argument shapes, all separator-free.
#: ``tap_<hex>``  a queued tool/spend approval (``tap-<ask id>``)
#: ``p_<hex>``    any pending item, by its short alias (``pending_tap_alias``)
#: ``all``        the whole queue
_SUFFIX_RE = re.compile(r"^(?:all|tap_[0-9a-zA-Z-]+|p_[0-9a-f]+)$", re.IGNORECASE)


def parse_tappable(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``("/approve", "tap-abc")`` for ``/approve_tap_abc``, else ``(None, None)``.

    Pure, so the parsing rule is testable without a chat round trip.
    """
    token = (text or "").strip().split()[0] if (text or "").strip() else ""
    for verb in TAPPABLE_VERBS:
        prefix = verb + "_"
        if token.lower().startswith(prefix) and len(token) > len(prefix):
            suffix = token[len(prefix):]
            if not _SUFFIX_RE.match(suffix):
                return None, None
            return verb, ("all" if suffix.lower() == "all"
                          else suffix.replace("_", "-"))
    return None, None


def is_tappable_token(token: str) -> bool:
    """True when ``token`` is a verb with its argument folded in.

    Used by the remedy checker: ``/approve_p_1245c6`` is not an invented verb,
    it is ``/approve`` carrying the item it decides.
    """
    return parse_tappable(token)[0] is not None
