"""Explicit state machines (proposal 098).

Small primitives every stateful row, file and object uses instead of ad hoc
copies: a transition table (:mod:`.machine`), guarded SQL transitions
(:mod:`.sql`), atomic/locked JSON files (:mod:`.files`), a latch that sets only
after success (:mod:`.once`), and a process identity for leases (:mod:`.proc`).

Tier-0: imports nothing above ``core``; stdlib + ``core.sqlite_util`` only.
"""
from core.state.machine import REGISTRY, IllegalTransition, StateMachine, strict_mode

__all__ = ["REGISTRY", "IllegalTransition", "StateMachine", "strict_mode"]
