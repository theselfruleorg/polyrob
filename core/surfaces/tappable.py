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
TAPPABLE_VERBS: Tuple[str, ...] = ("/approve", "/reject", "/pending")
#: ``/pending_p_<hex>`` opens ONE item's full review — a read, never a decision.

#: Accepted argument shapes, all separator-free.
#: ``tap_<hex>``  a queued tool/spend approval (``tap-<ask id>``)
#: ``p_<hex>``    any pending item, by its short alias (``pending_tap_alias``)
#: ``p_<hex>_<hex>`` the same, plus the draft revision the owner was shown
#:                (098 CO-19, ``core.self_evolution.pending_tap_token``)
#: ``all``        the whole queue
_SUFFIX_RE = re.compile(r"^(?:all|tap_[0-9a-zA-Z-]+|p_[0-9a-f]+(?:_[0-9a-f]+)?)$",
                        re.IGNORECASE)


#: 096 W5: READ tokens — one id folded into a token that OPENS one thing in
#: full. Each is a read, never a decision. ``token prefix -> (the verb it runs,
#: the id shape)``; the verb is a real chat verb that takes the id as its one
#: argument. The id is passed through AS TYPED (no ``_`` → ``-`` mapping):
#: ``/more``, ``/ask`` and ``/card`` ids are hex, and a skill id keeps its
#: underscores — the skills seat matches it against the live ids itself, so
#: no ``-``/``_`` guess is ever made here.
READ_TOKENS = {
    "/more": ("/more", re.compile(r"^[0-9a-f]{6,16}$")),
    "/ask": ("/asks", re.compile(r"^[0-9a-f]{6,24}$")),
    # A card READ is ``/card_<10 hex>``; ``/card_<id>_<act>`` (a press) never
    # matches this shape and stays ``core.surfaces.cards.parse_card_token``'s.
    "/card": ("/cards", re.compile(r"^[0-9a-f]{10}$")),
    "/skill": ("/skills", re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")),
}


def read_token(prefix: str, ident: str) -> str:
    """``/ask_<id>`` / ``/card_<id>`` / ``/more_<token>`` / ``/skill_<id>``.

    A skill id's ``-`` becomes ``_`` (a chat client links only ``[A-Za-z0-9_]``);
    the skills seat maps it back by matching the live ids. An id that cannot
    fold into the token's shape returns ``""`` — the caller then shows no tap
    rather than a dead one."""
    shape = READ_TOKENS.get(prefix)
    if shape is None:
        return ""
    folded = str(ident or "").strip().lower()
    if prefix == "/skill":
        folded = folded.replace("-", "_")
    return f"{prefix}_{folded}" if shape[1].match(folded) else ""


def _parse_read(token: str) -> Tuple[Optional[str], Optional[str]]:
    low = token.lower()
    for prefix, (verb, shape) in READ_TOKENS.items():
        head = prefix + "_"
        if low.startswith(head) and len(low) > len(head):
            suffix = low[len(head):]
            if shape.match(suffix):
                return verb, suffix
    return None, None


def parse_tappable(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``("/approve", "tap-abc")`` for ``/approve_tap_abc``, else ``(None, None)``.

    A READ token (:data:`READ_TOKENS`) parses to its verb and id:
    ``("/asks", "<id>")`` for ``/ask_<id>``.

    Pure, so the parsing rule is testable without a chat round trip.
    """
    token = (text or "").strip().split()[0] if (text or "").strip() else ""
    verb, arg = _parse_read(token)
    if verb is not None:
        return verb, arg
    for verb in TAPPABLE_VERBS:
        prefix = verb + "_"
        if token.lower().startswith(prefix) and len(token) > len(prefix):
            suffix = token[len(prefix):]
            if not _SUFFIX_RE.match(suffix):
                return None, None
            return verb, ("all" if suffix.lower() == "all"
                          else suffix.replace("_", "-"))
    return None, None


#: O14/A4: one option of an owner ask, folded into ONE token —
#: ``/fulfill_<ask id>_<letter>``. The ask id is the board's hex id (separator
#: free), the letter one of the options the ask stored when it was raised. A
#: tap is the owner's TYPED answer; the model never resolves it.
_ASK_OPTION_RE = re.compile(r"^/fulfill_([0-9a-f]{6,24})_([a-f])$", re.IGNORECASE)
ASK_OPTION_LETTERS = "ABCDEF"


def ask_option_token(ask_id: str, letter: str) -> str:
    """``/fulfill_<ask id>_<letter>`` (lowercase, the dispatcher's command shape)."""
    return f"/fulfill_{str(ask_id).lower()}_{str(letter).lower()}"


def parse_ask_option(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``("<ask id>", "A")`` for ``/fulfill_<ask id>_a``, else ``(None, None)``."""
    token = (text or "").strip().split()[0] if (text or "").strip() else ""
    m = _ASK_OPTION_RE.match(token)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2).upper()


def is_tappable_token(token: str) -> bool:
    """True when ``token`` is a verb with its argument folded in.

    Used by the remedy checker: ``/approve_p_1245c6`` is not an invented verb,
    it is ``/approve`` carrying the item it decides.
    """
    if parse_tappable(token)[0] is not None or parse_ask_option(token)[0] is not None:
        return True
    # Action cards (2026-09-27): ``/card_<id>_<act>`` names a stored card and
    # an act; what it does is read from the card row, never from the token.
    from core.surfaces.cards import parse_card_token
    return parse_card_token(token)[0] is not None


#: 096 E4: what an email reader is told when a notice carries chat taps. Email
#: cannot tap and owner-by-email is off, so the tokens are named as what they
#: are — commands for the chat seats — beside the two seats that need no chat.
EMAIL_TAP_FOOTER = ("—\nThe /… commands above work when you send them in Telegram or the "
                    "polyrob REPL. Or decide in the console Inbox, or run "
                    "`polyrob owner pending` on the host.")


def email_taps_footer(text: str) -> str:
    """``text`` plus :data:`EMAIL_TAP_FOOTER` when it holds any tappable token."""
    body = str(text or "")
    if EMAIL_TAP_FOOTER in body:
        return body
    for word in body.split():
        if is_tappable_token(word.strip(".,;:()[]")):
            return f"{body}\n\n{EMAIL_TAP_FOOTER}"
    return body
