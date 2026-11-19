"""121: the ONE rule for a model the AGENT chooses (a sub-agent, a goal, a job).

The agent may name any model of a provider that has a key. There is no price gate and no
owner card: the agent reads the prices (``agent_status(view="models")``), the result names
the price of each choice, and the run spend cap bounds the money. A model with no known price
runs with a warning. The owner's switch ``choice`` (``model.json``) narrows it: ``listed`` =
only the models the owner uses or listed.
"""
from typing import Optional, Tuple

CHOICE_FREE = "free"
CHOICE_LISTED = "listed"
CHOICE_MODES = (CHOICE_FREE, CHOICE_LISTED)

#: Verdicts.
ALLOW = "allow"
REFUSE = "refuse"

#: The one warning: a model with no known price is not refused.
NO_PRICE_WARNING = ("warning: its price is unknown, so only the run spend cap bounds its "
                    "cost")


def choice_mode(user_id: Optional[str], home_dir=None) -> str:
    """The owner's switch, ``free`` unless the owner set ``listed``. An unreadable store is
    ``listed``: the narrow answer when the owner's wish cannot be read."""
    from core import model_pins
    data, err = model_pins.load(user_id, home_dir) if user_id else ({}, "")
    if err:
        return CHOICE_LISTED
    mode = str(data.get("choice") or "").strip().lower()
    return mode if mode in CHOICE_MODES else CHOICE_FREE


def decide(*, listed: bool, mode: str,
           new_price: Optional[Tuple[float, float]]) -> Tuple[str, str]:
    """``(verdict, reason)`` for one named model. *listed* = the owner uses or listed it.
    *new_price* is ``(input, output)`` USD per million tokens, None = unknown. An ``ALLOW``
    with a reason is a warning the caller shows."""
    if listed:
        return ALLOW, ""
    if mode == CHOICE_LISTED:
        return REFUSE, "the owner allows only the models the owner uses or listed"
    if not new_price:
        return ALLOW, NO_PRICE_WARNING
    return ALLOW, ""
