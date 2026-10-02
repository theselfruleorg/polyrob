"""067 P1b: authorize_spend — the kernel refusals in one fixed order."""
import types

import pytest

import core.money.authority as authority
from core.money.authorize import (ACT, OWNER_QUEUE, REFUSED, SpendIntent,
                                  authorize_spend)


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: None)


def _ctx(**kw):
    d = dict(user_id="owner-1", role="orchestrator", is_sub_agent=False)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _genuine(ctx, tool):
    return False


def _forged(ctx, tool):
    return True


def test_a_genuine_owner_turn_acts():
    v = authorize_spend(SpendIntent(what="bridge", turn=True), _ctx(), forged_fn=_genuine)
    assert v.outcome == ACT and v.allowed and not v.autonomous


def test_a_stranger_is_refused_at_the_principal():
    v = authorize_spend(SpendIntent(what="bridge"), _ctx(user_id="stranger"))
    assert v.refused and v.step == "principal"


def test_a_leaf_reads_the_leaf_sentence_even_when_it_is_also_a_stranger():
    v = authorize_spend(SpendIntent(what="bridge"), _ctx(role="leaf", user_id="stranger"))
    assert v.step == "leaf" and "delegated sub-agent may not bridge" in v.reason


def test_leaf_text_is_the_authority_text():
    ctx = _ctx(is_sub_agent=True)
    v = authorize_spend(SpendIntent(what="deploy a token"), ctx)
    assert v.reason == authority.leaf_refusal(ctx, "deploy a token")


def test_the_turn_bar_refuses_a_forged_turn():
    v = authorize_spend(SpendIntent(turn=True), _ctx(), forged_fn=_forged)
    assert v.refused and v.step == "turn"


def test_the_autonomous_lane_admits_a_forged_turn_it_vouches_for():
    v = authorize_spend(SpendIntent(turn=True), _ctx(), forged_fn=_forged,
                        autonomous_ok_fn=lambda c, t: True)
    assert v.allowed and v.autonomous


def test_a_raising_autonomous_probe_refuses():
    def boom(c, t):
        raise RuntimeError("x")
    v = authorize_spend(SpendIntent(turn=True), _ctx(), forged_fn=_forged,
                        autonomous_ok_fn=boom)
    assert v.refused and v.step == "turn"


def test_a_context_without_a_detector_fails_closed():
    v = authorize_spend(SpendIntent(turn=True), _ctx())
    assert v.refused and v.step == "turn"


def test_a_raising_detector_fails_closed():
    def boom(c, t):
        raise RuntimeError("probe down")
    v = authorize_spend(SpendIntent(turn=True), _ctx(), forged_fn=boom)
    assert v.refused and v.step == "turn_probe" and v.detail == "probe down"


def test_no_context_is_a_direct_call_without_a_turn_to_judge():
    v = authorize_spend(SpendIntent(turn=True), None)
    assert v.allowed


def test_the_pause_refuses_a_real_run_but_not_a_dry_run(monkeypatch):
    seen = []

    def paused(entry=False):
        seen.append(entry)
        return "paused"
    monkeypatch.setattr(authority, "spend_pause_refusal", paused)
    assert authorize_spend(SpendIntent(entry=True), _ctx()).step == "pause"
    assert seen == [True]
    assert authorize_spend(SpendIntent(dry_run=True), _ctx()).allowed
    assert seen == [True]


def test_the_order_is_principal_leaf_turn_pause(monkeypatch):
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: "paused")
    ctx = _ctx()
    assert authorize_spend(SpendIntent(turn=True), ctx, forged_fn=_forged).step == "turn"
    admitted = authorize_spend(SpendIntent(turn=True), ctx, forged_fn=_forged,
                               autonomous_ok_fn=lambda c, t: True)
    assert admitted.step == "pause"            # the pause binds the agent's own run
    # ... and not the owner's own turn (2026-09-26, owner_direct_turn).
    assert authorize_spend(SpendIntent(turn=True), ctx, forged_fn=_genuine).allowed
    assert authorize_spend(SpendIntent(turn=True), _ctx(role="leaf"),
                           forged_fn=_forged).step == "leaf"


def test_session_binding_is_the_controller_rule():
    v = authorize_spend(SpendIntent(leaf=False, pause=False), None, session_user="owner-1")
    assert v.refused and "authenticated execution context" in v.reason
    v = authorize_spend(SpendIntent(leaf=False, pause=False), _ctx(user_id="other"),
                        session_user="owner-1")
    assert v.refused and "does not match its session" in v.reason
    v = authorize_spend(SpendIntent(leaf=False, pause=False), _ctx(), session_user="owner-1")
    assert v.allowed


def test_the_ledger_step(monkeypatch):
    from core.money.ledger import SpendLedger
    ledger = SpendLedger(max_per_tx_usd=5.0)
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    monkeypatch.setattr("core.wallet.submission_journal.unresolved", lambda: [])
    ok = SpendIntent(usd=1.0, venue="x", check_ledger=True, leaf=False, pause=False)
    assert authorize_spend(ok, _ctx(), ledger=ledger).allowed
    big = SpendIntent(usd=50.0, venue="x", check_ledger=True, leaf=False, pause=False)
    v = authorize_spend(big, _ctx(), ledger=ledger)
    assert v.refused and v.step == "ledger"
    unknown = SpendIntent(usd=None, venue="x", check_ledger=True, leaf=False, pause=False)
    assert authorize_spend(unknown, _ctx(), ledger=ledger).allowed


def test_the_lane_reuses_spend_exemption(monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    import core.config_policy.spend_lane as lane
    monkeypatch.setattr(lane, "autonomous_ceiling_usd", lambda: 10.0)
    verb = sorted(v for v in lane.DEFI_SPEND_VERBS
                  if v.endswith("_swap") and v not in lane._RISK_REDUCING_VERBS)[0]
    small = SpendIntent(action=verb, params={"max_spend_usd": 1.0, "dry_run": False}, leaf=False, pause=False)
    assert authorize_spend(small, _ctx()).outcome == ACT
    large = SpendIntent(action=verb, params={"max_spend_usd": 1000.0, "dry_run": False}, leaf=False, pause=False)
    assert authorize_spend(large, _ctx()).outcome == OWNER_QUEUE


def test_nothing_ever_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(authority, "turn_refusal", boom)
    assert authorize_spend(SpendIntent(), _ctx()).outcome == REFUSED


def test_raising_pause_probe_is_a_refusal(monkeypatch):
    def boom(**kw):
        raise RuntimeError("pause import failed")
    monkeypatch.setattr(authority, "spend_pause_refusal", boom)
    result = authorize_spend(SpendIntent(), _ctx())
    assert result.refused and result.step == "pause"
    assert "pause import failed" in result.reason


@pytest.mark.parametrize("decision", [None, object(), types.SimpleNamespace(allowed="yes")])
def test_malformed_ledger_verdict_fails_closed(decision):
    ledger = types.SimpleNamespace(check=lambda **kw: decision)
    result = authorize_spend(SpendIntent(usd=1, check_ledger=True, pause=False),
                             _ctx(), ledger=ledger)
    assert result.refused and result.step == "ledger"


def test_a_pause_only_intent_runs_no_principal(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("principal ran")
    monkeypatch.setattr(authority, "turn_refusal", boom)
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: "paused")
    v = authorize_spend(SpendIntent(principal=False, leaf=False), None)
    assert v.step == "pause" and v.reason == "paused"
