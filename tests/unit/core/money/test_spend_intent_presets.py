"""W5 (D3): the money callers pick a NAMED bar set, never a hand-mixed one."""
import pathlib
import re

from core.money.authorize import SpendIntent

REPO = pathlib.Path(__file__).resolve().parents[4]


def test_presets_are_the_bars_the_callers_used():
    assert SpendIntent.verb_gate("bridge") == SpendIntent(what="bridge", pause=False)
    assert SpendIntent.pause_recheck(entry=True) == SpendIntent(
        principal=False, leaf=False, entry=True)
    assert SpendIntent.principal_only() == SpendIntent(leaf=False, turn=False, pause=False)
    assert SpendIntent.turn_origin() == SpendIntent(
        principal=False, leaf=False, turn=True, pause=False)


def test_normalised_callers_use_the_presets():
    """bridge, liquidity, deploy and the wallet-authority hook pass a preset."""
    hand_mixed = re.compile(r"SpendIntent\((?:what=\w+|what=\"[^\"]*\"), pause=False\)"
                            r"|SpendIntent\(principal=False, leaf=False"
                            r"|SpendIntent\(leaf=False, turn=False, pause=False\)")
    for rel in ("tools/defi/bridge_verb.py", "tools/defi/lp_verbs.py",
                "tools/defi/deploy_verb.py", "tools/controller/wallet_authority.py"):
        text = (REPO / rel).read_text()
        assert not hand_mixed.search(text), rel


def test_tx_guard_turn_origin_asks_the_kernel():
    text = (REPO / "core/wallet/tx_guard_steps/turn.py").read_text()
    body = text[text.index("def step_2_turn_origin"):text.index("def step_2b_token_bound_account")]
    assert "authorize_spend(" in body and "SpendIntent.turn_origin()" in body
    assert "forged_fn(execution_context" not in body
