"""The ORDER of the one policy hook chain (W5, owner decision D3). Tier-0.

The chain is built by ``tools/controller/policy_chain.py`` (the Controller tier
and the owner seats) and, for the two session gates whose state the agent tier
owns, by :func:`install_session_gates` here. Both read :data:`ORDER`; the
combination rule is the hook pipeline's (``tools/controller/hooks.py``):
pre-hooks run in registration order, the FIRST refusal answers, and a
``closed`` hook that raises refuses.
"""

#: (name, tier, pre/post, fail mode), in registration order.
ORDER = (
    ("denylist", "controller", "pre", "closed"),
    ("wallet_authority", "controller", "pre", "closed"),
    ("financial_claim", "controller", "pre", "closed"),
    ("refusal_taint_gate", "controller", "pre", "closed"),
    ("refusal_taint_record", "controller", "post", "open"),
    ("effect_gate", "controller", "pre", "closed"),
    ("effect_record", "controller", "post", "open"),
    ("command_guard", "controller", "pre", "closed"),
    ("approval", "controller", "pre", "closed"),
    ("payment_approval", "controller", "pre", "closed"),
    ("correspondent_gate", "session", "pre", "closed"),
    ("room_gate", "session", "pre", "closed"),
)

#: The stages an owner SEAT call runs (``policy_chain.run_seat_action``). No
#: approval stage: the owner at an authenticated seat is the approver; no
#: wallet-authority stage: the seat itself requires the bound owner for every
#: non-read verb (``core.money.authority.network_verb_refusal``).
SEAT_STAGES = ("denylist", "refusal_taint_gate", "effect_gate", "effect_record",
               "refusal_taint_record")


def install_session_gates(controller, *, correspondent_hook, room_hook) -> None:
    """Register the ``session`` tier of :data:`ORDER`, after the Controller tier.

    The agent tier builds both hooks over its orchestrator and hands them in."""
    if controller is None or not hasattr(controller, "register_pre_tool_call_hook"):
        return
    controller.register_pre_tool_call_hook(correspondent_hook, fail_mode="closed")
    controller.register_pre_tool_call_hook(room_hook, fail_mode="closed")


__all__ = ["ORDER", "SEAT_STAGES", "install_session_gates"]
