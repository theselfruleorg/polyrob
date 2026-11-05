"""A reply must not send the owner to a command that does not exist (103).

Prod 2026-11-04: the agent told the owner to run ``/adopt``, a verb deleted three
weeks earlier. The owner typed it. A verb named in a reply is checked against the one
verb table; a token card line (``/card_<id>_ok``, ``/approve_p_<id>``) counts by its
first word. Pure.
"""
import re
from typing import Dict, List

#: A verb that existed and is gone, with what to do now. The owner sees this reply
#: when typing the verb, and the agent sees it when naming the verb.
RETIRED_VERBS: Dict[str, str] = {
    "/adopt": "A scheduled job needs no adoption: it runs as written. /job pause stops it.",
}

_VERB = re.compile(r"(?:^|(?<=[\s(`\"'*]))(/[a-z][a-z0-9_]{1,31})(?=$|[\s).,:;!?`\"'*])")
#: The tap tokens the seats mint. Not verbs in the table, and always valid by shape.
_TOKEN_PREFIXES = ("/card_", "/approve_", "/reject_", "/pending_", "/ask_", "/job_", "/goal_",
                   "/more_", "/skill_", "/fulfill_")
#: A buyer's seat answers these by code (``surfaces/telegram/storefront_ops.py``), before
#: the command table. core cannot import surfaces, so a test holds the two together.
STOREFRONT_VERBS = frozenset({"/offers", "/buy", "/order"})


def unknown_verbs(text: str) -> List[str]:
    """The ``/verb`` words in *text* that no seat routes, in order, once each."""
    from core.surfaces.dispatcher import command_names
    known = command_names() | STOREFRONT_VERBS
    out: List[str] = []
    for token in _VERB.findall(text or ""):
        if token in known or token in out or token.startswith(_TOKEN_PREFIXES):
            continue
        out.append(token)
    return out


def verb_note(verb: str) -> str:
    return RETIRED_VERBS.get(verb, "")
