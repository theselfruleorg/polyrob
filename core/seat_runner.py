"""The seat action runner, registered from the tools tier (the layering ratchet's hook rule).

``tools/controller/policy_chain.py`` registers ``(run_seat_action, SeatRefused)`` here when it
is imported; a runner that acts on its own (``tools/autonomous_act.py``: the
income rule's buy) reads it from here, so it does not import the policy chain and does not
pull it into the largest import cycle. No runner registered = the act refuses (fail closed).
"""
from typing import Any, Callable, Optional, Tuple

_RUNNER: Optional[Tuple[Callable[..., Any], type]] = None


def register_seat_runner(run: Callable[..., Any], refused: type) -> None:
    global _RUNNER
    _RUNNER = (run, refused)


def seat_runner() -> Optional[Tuple[Callable[..., Any], type]]:
    """``(run_seat_action, SeatRefused)``, or None when the policy chain is not loaded."""
    return _RUNNER


__all__ = ["register_seat_runner", "seat_runner"]
