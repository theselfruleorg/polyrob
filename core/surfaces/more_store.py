"""The continuation store behind ``/more`` (096 W5): every cut is one tap away.

A chat reply that caps a list or a body used to end in a dead tail — "see the
console or ask me", "Ask for the whole list." — a pointer to a seat the owner
was not holding, or to a sentence the agent then had to answer with a paid
turn. Now the renderer that cuts KEEPS the part it did not show here, for a
day, and the cut line ends in a token the chat client links whole:

    … (+12 more: /more_3f9a1c2b)

A tap on it is ``/more 3f9a1c2b`` (``core.surfaces.tappable``), which returns
the kept text. It is a READ: it never decides, and a token names only text
this process already rendered for the same tenant.

In process, on purpose: the kept text is a rendering of rows that still exist
in their own stores; a restart loses only the shortcut, and ``/more`` then says
the list expired and names the verb that renders it again. Per tenant: a token
read under another tenant is "not found", the same answer as a wrong token.

:func:`clip` is the other half of the rule — a cut that has nowhere to keep a
tail (a status line, a preview) still SAYS it is cut, with ``…``.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

#: How long a kept tail answers (proposal 096 W5: 24 h).
TTL_S = 24 * 3600
#: Tails kept per tenant; the oldest goes first. A chat seat produces a few a day.
MAX_PER_TENANT = 64
#: The token: hex, so ``/more_<token>`` is one linkable word (``tappable``).
_TOKEN_BYTES = 4

_LOCK = threading.Lock()
#: tenant -> token -> (expires_at, text)
_STORE: "dict[str, OrderedDict[str, Tuple[float, str]]]" = {}


def clip(text: object, n: int, marker: str = "…") -> str:
    """``text`` cut to ``n`` characters WITH a marker when it is longer.

    The marker counts toward ``n``, so the result never exceeds it."""
    s = "" if text is None else str(text)
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    return s[: max(0, n - len(marker))].rstrip() + marker


def more_token(token: str) -> str:
    """The tappable ``/more_<token>``."""
    return f"/more_{token}"


def _tenant(tenant: Optional[str]) -> str:
    return str(tenant or "").strip()


def stash(tenant: Optional[str], text: str, *, now: Optional[float] = None) -> str:
    """Keep ``text`` for ``tenant`` and return its token. Empty text is kept too
    (the caller decided there is a tail); a blank tenant still gets a token,
    readable only under the same blank tenant."""
    t = time.time() if now is None else now
    token = secrets.token_hex(_TOKEN_BYTES)
    key = _tenant(tenant)
    with _LOCK:
        bucket = _STORE.setdefault(key, OrderedDict())
        for k in [k for k, (exp, _txt) in bucket.items() if exp <= t]:
            del bucket[k]
        while token in bucket:
            token = secrets.token_hex(_TOKEN_BYTES)
        bucket[token] = (t + TTL_S, str(text or ""))
        while len(bucket) > MAX_PER_TENANT:
            bucket.popitem(last=False)
    return token


def fetch(tenant: Optional[str], token: str, *, now: Optional[float] = None) -> Optional[str]:
    """The kept text, or None (unknown, expired, or another tenant's)."""
    t = time.time() if now is None else now
    tok = str(token or "").strip().lower()
    with _LOCK:
        bucket = _STORE.get(_tenant(tenant))
        if not bucket or tok not in bucket:
            return None
        exp, text = bucket[tok]
        if exp <= t:
            del bucket[tok]
            return None
        return text


def cut_line(tenant: Optional[str], hidden: int, tail_text: str, *,
             what: str = "more", prefix: str = "… ") -> str:
    """The line that ends a cut: ``… (+N more: /more_<token>)``.

    ``hidden`` is what the reader is told is missing (rows, or characters when
    ``what`` says so). Never raises: a store failure still names the count."""
    try:
        token = stash(tenant, tail_text)
    except Exception:   # noqa: BLE001 — the count must survive a store fault
        return f"{prefix}(+{hidden} {what})"
    return f"{prefix}(+{hidden} {what}: {more_token(token)})"


def cut_text(tenant: Optional[str], text: str, n: int) -> str:
    """``text`` whole when it fits ``n``; else its head and a cut line whose
    tap returns the WHOLE text (the head again, so the reader never has to
    stitch two messages)."""
    s = str(text or "")
    if len(s) <= n:
        return s
    head = s[:n].rstrip()
    return head + "\n" + cut_line(tenant, len(s) - len(head), s, what="chars")


#: The reply when a token answers nothing. Names the verbs that render again.
EXPIRED = ("That continuation is gone — it lasts a day, and a restart clears it. "
           "Run the list again: /inbox, /pending, /recap, /goals, /asks.")


def more_reply(tenant: Optional[str], args: list) -> str:
    """``/more <token>`` (the tap ``/more_<token>``): the text a cut kept."""
    token = str((args or [""])[0] or "").strip().lower()
    if not token:
        return ("Usage: /more <token> — tap the /more_… link at the end of a "
                "list I cut short.")
    text = fetch(tenant, token)
    if text is None:
        return EXPIRED
    return text or "(nothing more)"


def _reset_for_tests() -> None:
    with _LOCK:
        _STORE.clear()


__all__ = ["EXPIRED", "MAX_PER_TENANT", "TTL_S", "clip", "cut_line", "cut_text",
           "fetch", "more_reply", "more_token", "stash"]
