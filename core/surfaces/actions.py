"""The action list (064 S2b F2): tappable choices as data.

Owner-facing notices already end in one-token commands the owner can tap
(``core.surfaces.tappable``: ``/approve_p_a1b2c3``, ``/approve_all``). A surface
that can render buttons shows those same commands as buttons; a surface that
cannot shows the text exactly as before.

ONE rule decides what a button may carry — :func:`is_action_command`: a tappable
token or a ``core/verbs.py`` verb, one token, nothing else. The envelope
(``core.surfaces.envelopes.Action``) enforces it at construction and a surface
re-checks it on the way back in (a callback payload is untrusted input).

⚠️ Buttons are EXPLICIT, never inferred from arbitrary text. An agent reply can
quote a correspondent's mail or a web page that carries ``/approve_p_…`` —
as plain auto-linked text that is incidental; as a green "Approve" button it
reads like a system decision card. So a renderer shows only
``OutboundMessage.actions``, and :func:`actions_from_text` is called ONLY by a
code-produced notice path: the approval notices (:data:`APPROVAL_NOTICE_SOURCES`,
in ``core/surfaces/user_delivery.py``) and the ``/pending`` reply. There the
tokens are the ones the producer itself wrote, so the text and the buttons
never disagree.
"""
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

#: A button's command is one token: the verb plus a folded argument at most.
_TOKEN_RE = re.compile(r"^/[A-Za-z0-9_]{1,64}$")

#: A tappable token inside prose (not part of a path or a longer word).
_TAPPABLE_IN_TEXT = re.compile(r"(?<![\w/])(/(?:approve|reject)_[A-Za-z0-9_-]+)")

#: How many buttons one message carries at most (a phone screen, not a menu).
MAX_ACTIONS = 6

#: ``send_user_message`` sources whose text is a CODE-PRODUCED decision card
#: (the tokens in it were written by the producer, not by the model). Only these
#: get buttons derived from their text.
APPROVAL_NOTICE_SOURCES = frozenset({
    "approval", "tool_approvals", "payment_approval", "self_evolution",
    "token_identity",  # W1: which contract is the real token (trust / not trusted)
})

#: O14/A4: an ``owner_ask`` notice. Its body carries the MODEL's question, so
#: approve/reject tokens are never derived from it; only the answer tokens for
#: THIS ask (``/fulfill_<ask id>_<letter>``, written by the raise path) become
#: buttons — see :func:`notice_actions`.
OWNER_ASK_SOURCE = "owner_ask"
_ASK_OPTION_IN_TEXT = re.compile(r"(?<![\w/])(/fulfill_[0-9a-f]{6,24}_[a-f])(?![\w])")

#: Owner verbs whose reply is a code-rendered decision list.
DECISION_LIST_COMMANDS = frozenset({"/pending"})


def is_action_command(command: str) -> bool:
    """True when ``command`` is something the owner could type as ONE token."""
    token = (command or "").strip()
    if not token or token != command or not _TOKEN_RE.match(token):
        return False
    from core.surfaces.tappable import is_tappable_token
    if is_tappable_token(token):
        return True
    from core.verbs import verb_for
    return verb_for(token) is not None


def _label_for(token: str) -> str:
    from core.surfaces.tappable import parse_tappable
    verb, arg = parse_tappable(token)
    if verb is None:
        return token
    word = "Approve" if verb == "/approve" else "Reject"
    return f"{word} all" if arg == "all" else f"{word} {arg}"


def _whole_queue(token: str) -> bool:
    from core.surfaces.tappable import parse_tappable
    return parse_tappable(token)[1] == "all"


def actions_from_text(text: str) -> List["object"]:
    """The tappable tokens ``text`` already offers, as ``Action``s, in order.

    ⚠️ TG9 (audit 2026-10-03): never a whole-queue button (``/approve_all``,
    ``/reject_all``). A button outlives its message and decides the queue as
    it is at TAP time — an old "Approve all" approved items its message never
    listed. Each item keeps its own button; the typed token stays in the text."""
    from core.surfaces.envelopes import Action
    out, seen = [], set()
    for token in _TAPPABLE_IN_TEXT.findall(text or ""):
        if token in seen or not is_action_command(token) or _whole_queue(token):
            continue
        seen.add(token)
        style = "primary" if token.startswith("/approve") else "danger"
        out.append(Action(label=_label_for(token), command=token, style=style))
        if len(out) >= MAX_ACTIONS:
            break
    return out


def actions_for(explicit: Optional[list]) -> list:
    """What a renderer shows: the explicit actions, re-checked. Never raises,
    never derives from text (see the module note)."""
    try:
        return [a for a in (explicit or [])
                if is_action_command(getattr(a, "command", ""))][:MAX_ACTIONS]
    except Exception:
        logger.debug("actions_for: no actions (fail-open)", exc_info=True)
        return []


def ask_option_actions(text: str, ask_id: str) -> list:
    """The answer buttons of ONE owner ask: every ``/fulfill_<ask_id>_<letter>``
    token in ``text`` whose id IS ``ask_id``. A token naming another ask (the
    question text is the model's) is dropped, so a button can only ever answer
    the ask the notice is about."""
    from core.surfaces.envelopes import Action
    from core.surfaces.tappable import parse_ask_option
    want = str(ask_id or "").lower()
    if not want:
        return []
    out, seen = [], set()
    for token in _ASK_OPTION_IN_TEXT.findall(text or ""):
        aid, letter = parse_ask_option(token)
        if aid != want or token in seen or not is_action_command(token):
            continue
        seen.add(token)
        out.append(Action(label=f"Answer {letter}", command=token, style="primary"))
        if len(out) >= MAX_ACTIONS:
            break
    return out


def notice_actions(source: str, text: str, ask_id: Optional[str] = None) -> list:
    """Buttons for a ``send_user_message`` notice: its own tokens when the
    source is a code-produced decision card, the answer buttons of its ask for
    an ``owner_ask`` notice, else none."""
    try:
        if source == OWNER_ASK_SOURCE:
            return ask_option_actions(text, ask_id or "")
        if source not in APPROVAL_NOTICE_SOURCES:
            return []
        return actions_from_text(text)
    except Exception:
        return []
