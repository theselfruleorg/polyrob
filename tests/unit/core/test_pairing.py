"""Ingress pairing / access-control (polyrob Phase D3).

Reference-style owner-allowlist + DM pairing, adapted to POLYROB's multi-tenant
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


def test_expired_code_cannot_be_approved_and_is_replaced(tmp_path, monkeypatch):
    import core.pairing as pairing
    clock = [1000.0]
    monkeypatch.setattr(pairing.time, "time", lambda: clock[0])
    store = _store(tmp_path)
    first = store.request("u1")
    clock[0] += pairing.PAIRING_CODE_TTL_SEC + 1
    assert store.approve(first) is None
    assert not store.list_pending()
    second = store.request("u1")
    assert first != second
    assert store.approve(second) == "u1"
    assert store.approve(second) is None


def test_pairing_store_failure_denies_remote_user(monkeypatch):
    import core.pairing as pairing
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    def failed(*a, **kw):
        raise OSError("unreadable")
    monkeypatch.setattr(pairing, "PairingStore", failed)
    decision = pairing.guard_inbound(None, "stranger", surface_id="telegram")
    assert decision is not None and not decision.allowed
    assert "unavailable" in decision.reason


# --- 098 CO-14: one live code per principal; expired rows purged -------------

def test_concurrent_requests_return_same_code(tmp_path, monkeypatch):
    """Two requests that both saw no live code: the conditional upsert keeps the
    first stored code, so both callers return it and it stays approvable."""
    import threading
    import core.pairing as pairing
    s = _store(tmp_path)
    codes = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(pairing.secrets, "token_hex", lambda n: next(codes))
    out = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        out.append(s.request("u1"))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out[0] == out[1]
    assert s.approve(out[0]) == "u1"


def test_expired_code_is_replaced(tmp_path, monkeypatch):
    import core.pairing as pairing
    s = _store(tmp_path)
    first = s.request("u1")
    real = pairing.time.time
    monkeypatch.setattr(pairing.time, "time", lambda: real() + pairing.PAIRING_CODE_TTL_SEC + 1)
    second = s.request("u1")
    assert second and second != first


def test_paired_user_gets_no_new_code(tmp_path):
    s = _store(tmp_path)
    s.approve(s.request("u1"))
    assert s.request("u1") is None
    assert s.is_paired("u1") is True


def test_purge_removes_only_unpaired_expired(tmp_path):
    from core.sqlite_util import execute_retry
    s = _store(tmp_path)
    s.approve(s.request("paired"))
    s.request("fresh")
    s.request("stale")
    execute_retry(s.db_path, "UPDATE paired_users SET created_at=0 "
                  "WHERE user_id IN ('stale','paired')")
    assert s.purge_expired() == 1
    left = {r["user_id"] for r in execute_retry(
        s.db_path, "SELECT user_id FROM paired_users", (), fetch="all")}
    assert left == {"paired", "fresh"}


def test_surface_gc_tick_purges_expired_pairing_rows(tmp_path):
    import types
    from core.sqlite_util import execute_retry
    from core.surfaces.gc import prune_surface_stores
    s = _store(tmp_path)
    s.request("stale")
    execute_retry(s.db_path, "UPDATE paired_users SET created_at=0")
    prune_surface_stores(types.SimpleNamespace(db_path=str(tmp_path / "surfaces.db")))
    assert execute_retry(s.db_path, "SELECT COUNT(*) AS n FROM paired_users",
                         (), fetch="one")["n"] == 0
