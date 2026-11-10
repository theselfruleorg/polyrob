"""Owner decision 2026-11-10: the signer's caps are the bound.

A priced send inside the hard caps is signed, whatever the agent's own chat dial
says: the signer asks for the on-box approval only above ``[caps] approve_above_usd``
(a file without the key: ``per_tx_usd``). A send the signer cannot value still needs
the on-box approval (``test_066_p2_signer_service.py``), and shadow counts such a hold
apart from a disagreement.
"""
import json

import pytest

from core.signer import protocol, shadow
from core.signer.caps import SignerConfigError, parse_signer_config, render_signer_toml
from tests.unit.core.signer.conftest import (AGENT_UID, AMOUNT, FakeRail, make_config,
                                             native_deltas, native_intent, native_tx)

BIG = 4 * AMOUNT          # ≈ $41: above the agent's default $25 dial, below the $50 cap


def _big(rig, **intent_kw):
    rig.deltas = native_deltas(native_delta=-BIG)
    return rig.body(native_intent(amount_raw=BIG, **intent_kw), native_tx(value=BIG))


def test_a_priced_send_above_the_chat_dial_is_signed_inside_the_caps(rig):
    resp = rig.call("evm.send", _big(rig))
    assert resp["ok"], resp
    assert resp["result"]["amount_usd"] > 25 and resp["result"]["lane"] == "autonomous"
    assert len(FakeRail.sent) == 1
    # booked against the signer's own daily cap
    assert rig.service.gate.audit_log[0]["amount_usd"] == pytest.approx(resp["result"]["amount_usd"])


def test_the_daily_cap_still_binds_such_sends(make_rig):
    rig = make_rig(per_tx_usd=50.0, daily_usd=60.0)
    assert rig.call("evm.send", _big(rig))["ok"]
    rig.nonces["next"] = 8
    rig.deltas = native_deltas(native_delta=-BIG)
    body = rig.body(native_intent(amount_raw=BIG, idempotency_key="native-2"),
                    native_tx(nonce=8, value=BIG))
    resp = rig.call("evm.send", body)
    assert resp["ok"] is False and resp["code"] == protocol.APPROVAL_REQUIRED
    assert len(FakeRail.sent) == 1


def test_root_can_set_a_lower_ask_above_value(make_rig):
    rig = make_rig(caps={"approve_above_usd": 30.0})
    resp = rig.call("evm.send", _big(rig))
    assert resp["ok"] is False and resp["code"] == protocol.APPROVAL_REQUIRED
    assert "polyrob owner promote signer_approval" in resp["reason"]
    assert FakeRail.sent == []
    rig.deltas = native_deltas()
    small = rig.call("evm.send", rig.body())                # the $10 send is below it
    assert small["ok"], small


def test_the_config_default_is_the_per_tx_cap_and_a_higher_value_refuses(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.approve_above_usd == cfg.per_tx_usd == 50.0
    assert cfg.summary()["approve_above_usd"] == 50.0
    low = make_config(tmp_path, caps={"approve_above_usd": 20.0})
    assert low.approve_above_usd == 20.0
    assert "approve_above_usd = 20.0" in render_signer_toml(low)
    import tomllib
    assert parse_signer_config(tomllib.loads(render_signer_toml(low))).approve_above_usd == 20.0
    with pytest.raises(SignerConfigError, match="approve_above_usd"):
        make_config(tmp_path, caps={"approve_above_usd": 51.0})


def test_the_agent_dial_stops_at_the_signer_value(monkeypatch):
    from core.wallet import signer_envelope, tx_guard
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "40")
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "100")
    monkeypatch.setattr(signer_envelope, "signer_caps", lambda: None)
    assert tx_guard.autonomous_max_usd() == 40.0
    monkeypatch.setattr(signer_envelope, "signer_caps",
                        lambda: {"per_tx_usd": 100.0, "approve_above_usd": 30.0})
    assert tx_guard.autonomous_max_usd() == 30.0
    # an older signer reports no value: nothing binds
    monkeypatch.setattr(signer_envelope, "signer_caps",
                        lambda: {"per_tx_usd": 100.0, "approve_above_usd": None})
    assert tx_guard.autonomous_max_usd() == 40.0


# -- shadow: a hold for the on-box approval is not a disagreement ------------------

def test_shadow_counts_an_approval_hold_apart(make_rig, tmp_path, monkeypatch):
    from tests.unit.core.signer.conftest import LoopbackClient
    monkeypatch.setattr(shadow, "shadow_log_path", lambda: str(tmp_path / "shadow.jsonl"))
    rig = make_rig(caps={"approve_above_usd": 30.0})
    from core.signer import attest

    class Local:
        def sign_transaction(self, tx):
            return b"signed"
    signer = shadow.ShadowEvmSigner(Local(), client=LoopbackClient(rig.service, AGENT_UID))
    tx = native_tx(value=BIG)
    rig.deltas = native_deltas(native_delta=-BIG)
    attest.record(native_intent(amount_raw=BIG), tx)
    assert signer.compare(tx) == "needs_approval"
    rows = [json.loads(l) for l in open(tmp_path / "shadow.jsonl")]
    assert rows[-1]["kind"] == "needs_approval" and rows[-1]["code"] == protocol.APPROVAL_REQUIRED
    counts = shadow.summary(7.0)["counts"]
    assert counts["needs_approval"] == 1 and counts["disagree"] == 0
    # the signer's own log says the same, and neither log blocks on it
    assert shadow.signer_verdict_summary(str(rig.state_dir))["counts"] == {
        "agree": 0, "disagree": 0, "needs_approval": 1}
    ok, why, _ = shadow.cutover_ready()
    assert not ok and "no agreed send" in why


def test_an_old_approval_row_is_read_as_a_hold(tmp_path, monkeypatch):
    import time
    path = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(shadow, "shadow_log_path", lambda: str(path))
    now = time.time()
    path.write_text("\n".join(json.dumps(r) for r in (
        {"ts": now - 60, "kind": "disagree", "code": "approval_required"},
        {"ts": now - 50, "kind": "disagree", "code": "guard_refused"},
        {"ts": now - 40, "kind": "agree"})) + "\n")
    st = shadow.summary(7.0, now=now)
    assert st["counts"]["needs_approval"] == 1 and st["counts"]["disagree"] == 1
    assert st["last_disagreement"]["code"] == "guard_refused"


def test_the_unverified_switch_is_root_config_and_defaults_to_approve(tmp_path):
    assert make_config(tmp_path).unverified_sends == "approve"
    cfg = make_config(tmp_path, policy={"unverified_sends": "caps"})
    assert cfg.unverified_sends == "caps" and cfg.summary()["unverified_sends"] == "caps"
    assert 'unverified_sends = "caps"' in render_signer_toml(cfg)
    with pytest.raises(SignerConfigError, match="unverified_sends"):
        make_config(tmp_path, policy={"unverified_sends": "yes"})

