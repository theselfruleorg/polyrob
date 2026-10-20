"""085 W4 (S-1) — the signer's own x402 caps and approval lane.

An EIP-3009 authorization skips ``tx_guard``, so the signer bounds it by its own
small per-payment cap (D4: ``X402_AUTONOMOUS_MAX_USD`` or $5, never
``per_tx_usd``) and an x402 venue daily cap. Above either, the same root-only
grant ``evm.send`` uses: an approval opens and nothing is signed.
"""
import time

import pytest

from core.signer import protocol
from core.signer.caps import (DEFAULT_X402_DAILY_USD, DEFAULT_X402_PER_PAYMENT_USD,
                              parse_signer_config, render_signer_toml)
from tests.unit.core.signer.conftest import AGENT_UID, make_config


def _x402(rig, *, usd, nonce_byte="ab", to="0x" + "c" * 40):
    from core.wallet import chains
    base = chains.get("base")
    now = int(time.time())
    return {"domain": {"name": "USD Coin", "version": "2", "chainId": base.chain_id,
                       "verifyingContract": base.usdc},
            "types": {"TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
            "primary_type": "TransferWithAuthorization",
            "message": {"from": rig.service.wallet.operational_signer().address, "to": to,
                        "value": int(round(usd * 10 ** 6)), "validAfter": now - 5,
                        "validBefore": now + 300, "nonce": "0x" + nonce_byte * 32}}


def _x402_rows(rig):
    return [e for e in rig.service.gate.audit_log if e["venue"] == "x402"]


# -- signer.toml: defaults and the old file ------------------------------------

def _data(caps):
    return {"caps": caps, "policy": {"chains": ["base"]},
            "server": {"client_uids": [AGENT_UID]}, "identity": {"network": "mainnet"}}


def test_missing_x402_keys_take_the_safe_per_payment_default_not_per_tx():
    cfg = parse_signer_config(_data({"per_tx_usd": 1000.0, "daily_usd": 3000.0}))
    assert cfg.x402_per_payment_usd == DEFAULT_X402_PER_PAYMENT_USD == 5.0


def test_a_file_without_x402_daily_keeps_the_daily_cap_as_the_venue_cap():
    """No new refusal on an existing box: the old behaviour was the daily cap."""
    cfg = parse_signer_config(_data({"per_tx_usd": 10.0, "daily_usd": 3000.0}))
    assert cfg.x402_daily_usd == 3000.0


def test_an_existing_provisioned_file_still_loads():
    """The prod file has no x402_daily_usd: it loads; the explicit per-payment
    value (root's) is kept; the venue cap takes its default."""
    cfg = parse_signer_config(_data({"per_tx_usd": 1000.0, "daily_usd": 3000.0,
                                     "x402_per_payment_usd": 1000.0}))
    assert cfg.x402_per_payment_usd == 1000.0
    assert cfg.x402_daily_usd == 3000.0


def test_render_round_trips_the_x402_daily_cap(tmp_path):
    cfg = make_config(tmp_path, caps={"x402_daily_usd": 7.5})
    import tomllib
    again = parse_signer_config(tomllib.loads(render_signer_toml(cfg)))
    assert again.x402_daily_usd == 7.5
    assert "x402_daily_usd" in cfg.summary()


# -- provisioning (D4) ---------------------------------------------------------

def test_provision_x402_cap_from_the_autonomous_ceiling():
    from core.signer.provision import x402_caps
    out = x402_caps({"X402_AUTONOMOUS_MAX_USD": "1.0"}, 3000.0)
    assert out["x402_per_payment_usd"] == 1.0


def test_provision_x402_cap_default_is_five_never_per_tx():
    from core.signer.provision import x402_caps
    out = x402_caps({"AGENT_WALLET_MAX_PER_TX_USD": "1000"}, 3000.0)
    assert out["x402_per_payment_usd"] == 5.0
    assert out["x402_daily_usd"] == DEFAULT_X402_DAILY_USD
    for bad in ("nan", "inf", "-1", "0", "x"):
        assert x402_caps({"X402_AUTONOMOUS_MAX_USD": bad}, 3000.0)["x402_per_payment_usd"] == 5.0


def test_provision_venue_cap_from_the_agent_venue_cap():
    from core.signer.provision import x402_caps
    out = x402_caps({"WALLET_VENUE_DAILY_CAP_X402_USD": "12"}, 3000.0)
    assert out["x402_daily_usd"] == 12.0


# -- the approval lane ---------------------------------------------------------

def test_a_payment_within_the_caps_signs_unchanged(rig):
    resp = rig.call("x402.authorize", _x402(rig, usd=1.5))
    assert resp["ok"], resp
    assert len(_x402_rows(rig)) == 1


def test_x402_at_per_tx_opens_an_approval_not_a_signature(rig):
    """The review probe: value = per_tx_usd ($50) to an arbitrary payee."""
    resp = rig.call("x402.authorize", _x402(rig, usd=50.0))
    assert resp["ok"] is False and resp["code"] == protocol.APPROVAL_REQUIRED, resp
    assert resp.get("approval_id", "").startswith("sig-")
    assert "signature" not in resp
    assert _x402_rows(rig) == []
    pending = rig.service.store.pending()
    assert len(pending) == 1 and pending[0]["op"] == "x402.authorize"
    assert "0x" + "c" * 40 in pending[0]["summary"]


def test_the_agent_cannot_grant_and_a_root_grant_unlocks_one_retry(rig):
    resp = rig.call("x402.authorize", _x402(rig, usd=50.0))
    approval_id = resp["approval_id"]
    denied = rig.call("approvals.decide", {"id": approval_id, "grant": True})
    assert denied["ok"] is False and denied["code"] == protocol.NOT_ROOT
    assert rig.call("approvals.decide", {"id": approval_id, "grant": True}, uid=0)["ok"]
    # The payer draws a fresh nonce and window on the retry: the grant still applies.
    retry = rig.call("x402.authorize", _x402(rig, usd=50.0, nonce_byte="cd"))
    assert retry["ok"], retry
    assert len(_x402_rows(rig)) == 1
    # One use: the next identical payment needs a new approval.
    again = rig.call("x402.authorize", _x402(rig, usd=50.0, nonce_byte="ef"))
    assert again["ok"] is False and again["code"] == protocol.APPROVAL_REQUIRED
    assert again["approval_id"] != approval_id


def test_a_grant_does_not_cover_another_payee(rig):
    resp = rig.call("x402.authorize", _x402(rig, usd=50.0))
    rig.call("approvals.decide", {"id": resp["approval_id"], "grant": True}, uid=0)
    other = rig.call("x402.authorize", _x402(rig, usd=50.0, nonce_byte="cd",
                                             to="0x" + "d" * 40))
    assert other["ok"] is False and other["code"] == protocol.APPROVAL_REQUIRED


def test_the_x402_venue_daily_cap_routes_to_approval(make_rig):
    rig = make_rig(caps={"x402_daily_usd": 3.0})
    assert rig.call("x402.authorize", _x402(rig, usd=1.5, nonce_byte="01"))["ok"]
    assert rig.call("x402.authorize", _x402(rig, usd=1.5, nonce_byte="02"))["ok"]
    third = rig.call("x402.authorize", _x402(rig, usd=1.5, nonce_byte="03"))
    assert third["ok"] is False and third["code"] == protocol.APPROVAL_REQUIRED
    assert "venue 'x402' daily cap" in third["reason"]


def test_a_replayed_nonce_still_refuses_outright(rig):
    assert rig.call("x402.authorize", _x402(rig, usd=1.5))["ok"]
    again = rig.call("x402.authorize", _x402(rig, usd=1.5))
    assert again["ok"] is False and again["code"] == protocol.GUARD_REFUSED
    assert rig.service.store.pending() == []


def test_a_fresh_provision_writes_the_tight_venue_cap(tmp_path):
    """Only provisioning writes the tight value; the rendered file carries it."""
    import tomllib
    from core.signer.caps import SignerConfig
    from core.signer.provision import x402_caps
    caps = x402_caps({}, 3000.0)
    cfg = SignerConfig(per_tx_usd=1000.0, daily_usd=3000.0, chains=("base",),
                       socket="/s", state_dir="/d", client_uids=(AGENT_UID,), **caps)
    again = parse_signer_config(tomllib.loads(render_signer_toml(cfg)))
    assert again.x402_daily_usd == DEFAULT_X402_DAILY_USD
    assert again.x402_per_payment_usd == 5.0
