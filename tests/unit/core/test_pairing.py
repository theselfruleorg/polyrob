"""Ingress pairing / access-control (polyrob Phase D3).

Hermes/OpenClaw-style owner-allowlist + DM pairing, adapted to ROB's multi-tenant
model: an unknown user is denied and issued a one-time pairing code; the operator
approves it out-of-band; owner/local are always allowed. Gated POLYROB_REQUIRE_PAIRING
(default OFF → everyone allowed → byte-identical).
"""
from core.pairing import (
    PairingStore,
    pairing_required,
    evaluate_access,
)


def _store(tmp_path):
    return PairingStore(str(tmp_path / "pairing.db"))


def test_pairing_not_required_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_REQUIRE_PAIRING", raising=False)
    assert pairing_required(env={}) is False


def test_pairing_required_env_on():
    assert pairing_required(env={"POLYROB_REQUIRE_PAIRING": "true"}) is True


def test_request_then_approve_flow(tmp_path):
    s = _store(tmp_path)
    assert s.is_paired("u1") is False
    code = s.request("u1")
    assert code and isinstance(code, str)
    assert s.is_paired("u1") is False          # not yet approved
    assert s.approve(code) == "u1"
    assert s.is_paired("u1") is True


def test_pairing_code_has_64bit_entropy(tmp_path):
    # IMP-2: 16 hex chars (64-bit), not 8 (32-bit).
    code = _store(tmp_path).request("u1")
    assert len(code) == 16


def test_request_is_stable_for_pending_user(tmp_path):
    s = _store(tmp_path)
    assert s.request("u1") == s.request("u1")   # same pending code, not a new one


def test_approve_unknown_code_returns_none(tmp_path):
    assert _store(tmp_path).approve("nope") is None


def test_revoke_unpairs(tmp_path):
    s = _store(tmp_path)
    s.approve(s.request("u1"))
    assert s.is_paired("u1")
    s.revoke("u1")
    assert s.is_paired("u1") is False


def test_evaluate_access_allowed_when_not_required(tmp_path):
    d = evaluate_access("anyone", store=_store(tmp_path), required=False)
    assert d.allowed


def test_evaluate_access_local_always_allowed(tmp_path):
    d = evaluate_access("anyone", store=_store(tmp_path), required=True, local=True)
    assert d.allowed and d.reason == "owner"


def test_evaluate_access_owner_allowed(tmp_path):
    d = evaluate_access("u-owner", store=_store(tmp_path), required=True,
                        owner_principal="u-owner")
    assert d.allowed and d.reason == "owner"


def test_evaluate_access_unknown_user_denied_with_code(tmp_path):
    s = _store(tmp_path)
    d = evaluate_access("stranger", store=s, required=True)
    assert not d.allowed
    assert d.pairing_code  # a code is issued so the operator can approve
    assert s.is_paired("stranger") is False


def test_evaluate_access_anon_denied_no_code(tmp_path):
    d = evaluate_access("", store=_store(tmp_path), required=True)
    assert not d.allowed
    assert not d.pairing_code  # an unidentifiable user cannot be paired


def test_evaluate_access_after_approval_allowed(tmp_path):
    s = _store(tmp_path)
    d1 = evaluate_access("u2", store=s, required=True)
    assert not d1.allowed
    s.approve(d1.pairing_code)
    d2 = evaluate_access("u2", store=s, required=True)
    assert d2.allowed and d2.reason == "paired"
