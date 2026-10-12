"""The money authorizer's step ORDER is policy (W6, 2026-10-10).

``tx_guard._authorize`` runs ``core.wallet.tx_guard_steps.STEPS`` in order and
the first step that returns a Decision answers. Which refusal a transaction
gets — and whether a pause, the turn origin or the simulation answers first —
depends on that order, so it is pinned here by name: a reorder, a new step or a
dropped step is a visible diff in this file. The outcomes themselves are pinned
by ``test_tx_guard_golden.py``.
"""
import ast
from pathlib import Path

from core.wallet import tx_guard, tx_guard_steps

EXPECTED_ORDER = (
    "step_0_owner_authority",
    "step_1_owner_pause",
    "step_1b_entry_pause",
    "step_2_turn_origin",
    "step_2b_token_bound_account",
    "step_3_structural",
    "step_3a_lifi_floor",
    "step_3_collection_shapes",
    "step_3_registration_shape",
    "step_3_nft_and_revoke_shapes",
    "step_3_claim_deploy_destination",
    "step_4_rpc_trust",
    "step_5_simulate",
    "step_6_assert_deltas",
    "step_6a_min_inflow",
    "step_6b_event_log",
    "step_6cde_non_fungibles",
    "step_6g_account_nesting",
    "step_6g2_future_account",
    "step_6g1_pinned_departure",
    "step_6f_registration_receipt",
    "step_6_native_balance",
    "step_7_price",
    "step_8_policy_gate",
    "step_9_approval_lane",
)

STEPS_DIR = Path(tx_guard_steps.__file__).parent


def test_the_step_order_is_pinned():
    assert tuple(step.__name__ for step in tx_guard_steps.STEPS) == EXPECTED_ORDER


def test_every_step_is_listed_once():
    assert len(set(tx_guard_steps.STEPS)) == len(tx_guard_steps.STEPS)


def test_authorize_runs_the_steps_and_stops_at_the_first_decision(monkeypatch):
    """The runner, not a copy of the guard: a refusal from the FIRST step ends it."""
    seen = []
    first = tx_guard_steps.STEPS[0]

    def refuse(ctx):
        seen.append(first.__name__)
        return tx_guard.Decision(False, "refused: pinned by the order test")

    def must_not_run(ctx):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("a step after a refusal ran")

    monkeypatch.setattr(tx_guard_steps, "STEPS", (refuse,) + (must_not_run,) * 3)
    d = tx_guard.authorize(intent=None, tx={}, holder="0x0", gate=None)
    assert seen == [first.__name__]
    assert d == tx_guard.Decision(False, "refused: pinned by the order test")


def test_steps_reach_tx_guard_helpers_through_the_module():
    """Tests and callers monkeypatch helpers on ``core.wallet.tx_guard``; a step
    module that imported one by name would bind the original and silently skip
    the patch. Only ``Decision`` (a class nobody patches) is imported by name."""
    for path in sorted(STEPS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.wallet.tx_guard":
                names = sorted(a.name for a in node.names)
                assert names == ["Decision"], f"{path.name} imports {names} by name"
