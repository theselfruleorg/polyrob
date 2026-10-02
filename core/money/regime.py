"""The money regime SHAPE (067 P1b): supervised / autonomous / armed.

The kernel owns the three names and the rule that composes them; the keys that
arm a money rail are contributed by that rail with :func:`register_arming_keys`
(today ``core/config_policy/money_regime.py`` registers the DeFi keys and the
daily cap). ``autonomous`` vs ``supervised`` is an AUTONOMY fact
(``core.config_policy.autonomy_mode.full_autonomy_enabled``), passed in.

    supervised — not effectively autonomous;
    autonomous — autonomous, and at least one registered arming key is unset
                 (``missing`` names them, in registration order);
    armed      — autonomous, and every registered arming key is set.

A check that raises counts as UNSET (fail-closed: never armed by an error).

Tier-0: stdlib only.
"""
from typing import Callable, Dict, NamedTuple, Tuple

REGIME_SUPERVISED = "supervised"
REGIME_AUTONOMOUS = "autonomous"
REGIME_ARMED = "armed"


class MoneyRegime(NamedTuple):
    name: str
    #: The keys still unset for ``armed`` (empty when armed; empty under
    #: ``supervised``, where arming money is not the next step).
    missing: Tuple[str, ...]


_ARMING: Dict[str, Callable[[], bool]] = {}


def register_arming_keys(checks: Dict[str, Callable[[], bool]]) -> None:
    """Add arming keys: ``{key_name: () -> is_set}``. Registration order is
    the order ``missing`` reports. Re-registering a key replaces its check."""
    for key, check in checks.items():
        if not callable(check):
            raise TypeError(f"arming key {key!r}: check is not callable")
        _ARMING[str(key)] = check


def arming_keys() -> Tuple[str, ...]:
    return tuple(_ARMING)


def _is_set(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


def resolve_regime(autonomous: bool) -> MoneyRegime:
    """The regime for this autonomy answer and the registered arming keys."""
    if not autonomous:
        return MoneyRegime(REGIME_SUPERVISED, ())
    missing = tuple(key for key, check in _ARMING.items() if not _is_set(check))
    if missing or not _ARMING:
        # No rail registered any key: nothing can be armed, so the instance is
        # autonomous with money NOT armed (and nothing to name as missing).
        return MoneyRegime(REGIME_AUTONOMOUS, missing)
    return MoneyRegime(REGIME_ARMED, ())
