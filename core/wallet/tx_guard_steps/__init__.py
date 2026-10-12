"""The money authorizer, as ordered steps over one context (W6, 2026-10-10).

``core.wallet.tx_guard._authorize`` was one function of ~1400 lines with
numbered step comments. It is now :data:`STEPS`, run in order by :func:`run`:
the first step that returns a :class:`~core.wallet.tx_guard.Decision` answers,
and a transaction that clears every step gets the final allow. The order IS
the policy (the pause before the turn origin, structure before any RPC, the
simulation before the deltas, the price before the caps) and it is pinned by
``tests/unit/core/wallet/test_tx_guard_step_order.py`` — a reorder is a visible
diff there. The exact outcomes are pinned by ``test_tx_guard_golden.py``.

⚠️ ``polyrob-signer`` re-runs this same code in its own process; keep these
modules free of new module-level imports (the signer import closure).
"""
from core.wallet.tx_guard_steps import measure, shape, turn, value
from core.wallet.tx_guard_steps.context import GuardContext

#: The guard, in order. One entry per numbered step of the original function.
STEPS = (
    turn.step_0_owner_authority,
    turn.step_1_owner_pause,
    turn.step_1b_entry_pause,
    turn.step_2_turn_origin,
    turn.step_2b_token_bound_account,
    shape.step_3_structural,
    shape.step_3a_lifi_floor,
    shape.step_3_collection_shapes,
    shape.step_3_registration_shape,
    shape.step_3_nft_and_revoke_shapes,
    shape.step_3_claim_deploy_destination,
    shape.step_4_rpc_trust,
    measure.step_5_simulate,
    measure.step_6_assert_deltas,
    measure.step_6a_min_inflow,
    measure.step_6b_event_log,
    measure.step_6cde_non_fungibles,
    measure.step_6g_account_nesting,
    measure.step_6g2_future_account,
    measure.step_6g1_pinned_departure,
    measure.step_6f_registration_receipt,
    measure.step_6_native_balance,
    value.step_7_price,
    value.step_8_policy_gate,
    value.step_9_approval_lane,
)


def run(ctx: GuardContext):
    """Run :data:`STEPS` in order; the first Decision answers, else the final allow."""
    for step in STEPS:
        decision = step(ctx)
        if decision is not None:
            return decision
    return value.allow(ctx)


__all__ = ["GuardContext", "STEPS", "run"]
