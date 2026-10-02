"""067 P1b: each caller switched to authorize_spend keeps today's decisions
and sentences. One section per caller, added with its switch."""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


# -- the controller wallet-authority pre-hook ------------------------------

def _hook(user_id="owner"):
    from tools.controller.wallet_authority import make_wallet_authority_hook
    return make_wallet_authority_hook(
        SimpleNamespace(user_id=user_id, get_action_details=lambda n: None))


def test_hook_non_money_actions_pass():
    assert _hook()("defi_data_quote", {}, None) is None


def test_hook_requires_a_context():
    assert _hook()("defi_trade_transfer", {}, None) == (
        "money actions require an authenticated execution context")


def test_hook_a_stranger_session_is_refused_even_without_a_context():
    assert _hook("stranger")("defi_trade_transfer", {}, None) == (
        "operator wallet access requires the bound owner identity")


def test_hook_identity_mismatch_overrides_every_other_sentence():
    ctx = SimpleNamespace(user_id="someone")
    assert _hook("stranger")("defi_trade_transfer", {}, ctx) == (
        "money action identity does not match its session")
    assert _hook()("defi_trade_transfer", {}, ctx) == (
        "money action identity does not match its session")


def test_hook_the_owner_turn_passes_and_a_leaf_is_not_refused_here():
    # The leaf / forged / pause bars belong to the verb and its guard.
    assert _hook()("defi_trade_transfer", {}, SimpleNamespace(user_id="owner")) is None
    assert _hook()("defi_trade_transfer", {},
                   SimpleNamespace(user_id="owner", role="leaf", is_sub_agent=True)) is None


# -- the x402 spend gate ----------------------------------------------------

@pytest.mark.parametrize("forged, ctx, expected", [
    (False, None, None),
    (True, None, None),                                   # a direct call: no turn
    (False, SimpleNamespace(user_id="owner"), None),
    (True, SimpleNamespace(user_id="owner"), "cannot spend"),
    (False, SimpleNamespace(user_id="x"), "bound owner identity"),
    (True, SimpleNamespace(user_id="x"), "bound owner identity"),  # principal first
])
def test_x402_gate_order_and_sentences(monkeypatch, forged, ctx, expected):
    from tools.x402 import spend_gate
    monkeypatch.setattr(spend_gate, "_forged_fn", lambda c, t: forged)
    got = spend_gate.x402_spend_refusal(ctx, None)
    assert (got is None) if expected is None else (expected in got)


def test_x402_gate_raising_detector_names_the_error(monkeypatch):
    from tools.x402 import spend_gate

    def boom(c, t):
        raise RuntimeError("probe down")
    monkeypatch.setattr(spend_gate, "_forged_fn", boom)
    got = spend_gate.x402_spend_refusal(SimpleNamespace(user_id="owner"), None)
    assert got == ("payment refused: could not prove the turn is genuine (probe down); "
                   "failing closed")


# -- the leaf + pause verbs --------------------------------------------------
# Each verb keeps: the leaf sentence first (unsuffixed), then the principal,
# then — on a real run only — the pause sentence with the verb's own suffix.

LEAF = SimpleNamespace(user_id="owner", role="leaf", is_sub_agent=True)
STRANGER = SimpleNamespace(user_id="x", role="orchestrator", is_sub_agent=False)
OWNER = SimpleNamespace(user_id="owner", role="orchestrator", is_sub_agent=False)
#: The owner's tenant, but a forged/autonomous turn: the pause binds it. A genuine
#: owner turn (OWNER) is the owner acting, and the pause does not (2026-09-26).
AUTO = SimpleNamespace(user_id="owner", role="orchestrator", is_sub_agent=False,
                       metadata={"turn_kind": "self_wake"})


@pytest.fixture
def paused(monkeypatch):
    import core.money.authority as auth
    seen = []

    def _paused(entry=False):
        seen.append(entry)
        return "PAUSED."
    monkeypatch.setattr(auth, "spend_pause_refusal", _paused)
    return seen


def _launchpad():
    from tools.launchpad.tool import LaunchpadTool
    return object.__new__(LaunchpadTool)


def test_launchpad_preflight(monkeypatch, paused):
    monkeypatch.setattr("tools.launchpad.tool.launchpad_enabled", lambda: True)
    t = _launchpad()
    assert "delegated sub-agent may not buy" in t._preflight(LEAF, "buy", dry_run=False)
    assert "bound owner" in t._preflight(STRANGER, "buy", dry_run=False)
    assert t._preflight(AUTO, "buy", dry_run=False) == "PAUSED. RESULT: NOT SENT."
    assert t._preflight(AUTO, "sell", dry_run=False, entry=False)
    assert paused == [True, False]
    assert t._preflight(OWNER, "buy", dry_run=False) is None   # the owner is not bound
    assert t._preflight(AUTO, "buy", dry_run=True) is None
    assert paused == [True, False]           # a dry run never asks the pause


def test_agent_nft_gate(monkeypatch, paused):
    from tools.agent_nft.tool import AgentNftTool
    monkeypatch.setattr("tools.agent_nft.tool.agent_nft_enabled", lambda: True)
    t = object.__new__(AgentNftTool)
    t._impl_override = object()
    assert "delegated sub-agent may not mint" in t._gate(LEAF, "mint", write=True)
    assert t._gate(AUTO, "mint", write=True, dry_run=False) == "PAUSED. RESULT: NOT SENT."
    assert t._gate(OWNER, "mint", write=True, dry_run=False) is None   # the owner is not bound
    assert t._gate(AUTO, "snapshot", write=False, dry_run=False) is None
    assert t._gate(AUTO, "mint", write=True, dry_run=True) is None
    assert paused == [True]


@pytest.mark.parametrize("helper", ["deploy", "bridge"])
def test_split_helpers_keep_their_halves(monkeypatch, paused, helper):
    from tools.defi import bridge_verb, deploy_verb
    turn = (lambda c: deploy_verb._refuse_non_owner_turn(c, "deploy token")) \
        if helper == "deploy" else bridge_verb._refuse_non_owner_turn
    pause = (lambda: deploy_verb._refuse_paused(entry=True)) \
        if helper == "deploy" else bridge_verb._refuse_paused
    assert "delegated sub-agent" in turn(LEAF)
    assert "bound owner" in turn(STRANGER)
    assert turn(OWNER) is None                 # the leaf half never asks the pause
    assert paused == []
    assert pause() == "PAUSED."
    assert paused == [True]
