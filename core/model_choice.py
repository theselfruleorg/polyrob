"""121: the ONE rule for a model the AGENT chooses (a sub-agent, a goal, a job).

The agent may name any model of a provider that has a key. There is no price gate and no
owner card: the agent reads the prices (``agent_status(view="models")``), the result names
the price of each choice, and the run spend cap bounds the money. A model with no known price
runs with a warning, and the meter charges it at its provider's highest known price (the
warning says so). The owner's switch ``choice`` (``model.json``) narrows it: ``listed`` =
only the models the owner uses or listed. The OWNER's switch binds every tenant: the
operator's keys pay for a tenant's runs too.
"""
from typing import Optional, Tuple

CHOICE_FREE = "free"
CHOICE_LISTED = "listed"
CHOICE_MODES = (CHOICE_FREE, CHOICE_LISTED)

#: Verdicts.
ALLOW = "allow"
REFUSE = "refuse"

#: The warning for a model with no known price and no ceiling to meter it at: the meter
#: records $0 for it unless the provider reports what it billed. It is not refused.
NO_PRICE_WARNING = ("warning: its price is unknown and I know no price of its provider, so "
                    "it is metered at $0 unless the provider reports what it billed; the run "
                    "spend cap may not see its cost")


def no_price_warning(ceiling: Optional[Tuple[float, float]] = None) -> str:
    """The warning for a model with no known price. *ceiling* is the ``(input, output)``
    USD per million tokens the meter charges it at (the highest known price of its
    provider), None = no ceiling."""
    if not ceiling:
        return NO_PRICE_WARNING
    return (f"warning: its price is unknown, so it is metered at the highest known price of "
            f"its provider (${ceiling[0]:g}/${ceiling[1]:g} per M tokens in/out) unless the "
            f"provider reports what it billed")


def _own_mode(user_id: Optional[str], home_dir=None) -> str:
    from core import model_pins
    data, err = model_pins.load(user_id, home_dir) if user_id else ({}, "")
    if err:
        return CHOICE_LISTED
    mode = str(data.get("choice") or "").strip().lower()
    return mode if mode in CHOICE_MODES else CHOICE_FREE


def choice_mode(user_id: Optional[str], home_dir=None) -> str:
    """The switch for *user_id*: ``free`` unless the tenant or the OWNER set ``listed`` (the
    owner's narrowing binds every tenant: the operator's keys pay). An unreadable store is
    ``listed``: the narrow answer when the wish cannot be read."""
    if _own_mode(user_id, home_dir) == CHOICE_LISTED:
        return CHOICE_LISTED
    try:
        from core.instance import is_owner_uid, resolve_owner_principal
        if is_owner_uid(user_id, allow_local=False):
            return CHOICE_FREE
        owner = resolve_owner_principal()
    except Exception:  # an unresolvable owner cannot widen anything: narrow
        return CHOICE_LISTED
    return _own_mode(owner, home_dir) if owner else CHOICE_FREE


def decide(*, listed: bool, mode: str,
           new_price: Optional[Tuple[float, float]],
           ceiling: Optional[Tuple[float, float]] = None) -> Tuple[str, str]:
    """``(verdict, reason)`` for one named model. *listed* = the owner uses or listed it.
    *new_price* is ``(input, output)`` USD per million tokens, None = unknown; *ceiling* is
    the price the meter charges an unknown-price model at (None = none). An ``ALLOW`` with
    a reason is a warning the caller shows."""
    if listed:
        return ALLOW, ""
    if mode == CHOICE_LISTED:
        return REFUSE, "the owner allows only the models the owner uses or listed"
    if not new_price:
        return ALLOW, no_price_warning(ceiling)
    return ALLOW, ""
