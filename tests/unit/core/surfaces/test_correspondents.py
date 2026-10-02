"""WS-A correspondent registry — the sole routing authority for third-party replies.

Security invariants (Fusion-validated must-fixes):
- registry maps (surface, address[, thread]) -> the session that INITIATED contact;
- an UNKNOWN sender never resolves (thread-hijack defense): forging a reply into an
  existing thread from a different address resolves to nothing;
- owner-provenance seeds may go active; untrusted-provenance seeds are ALWAYS pending
  (no self-bootstrapped trust from injected content);
- approval gate: a pending binding is NOT routable until approved;
- TTL: an expired binding stops resolving;
- per-tenant seed counting supports the per-day new-correspondent cap.
"""
import os
import tempfile

import pytest

from core.surfaces.correspondents import CorrespondentRegistry


@pytest.fixture()
def reg():
    d = tempfile.mkdtemp()
    yield CorrespondentRegistry(os.path.join(d, "correspondents.db"))


def test_owner_seed_without_approval_is_active_and_resolves(reg):
    reg.seed(surface="email", address="John@Acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False)
    row = reg.resolve(surface="email", address="john@acme.com", thread_id="t1")
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u_owner"


def test_owner_seed_with_approval_is_pending_until_approved(reg):
    state = reg.seed(surface="email", address="john@acme.com", session_id="s1",
                     user_id="u_owner", thread_id="t1", provenance="owner",
                     require_approval=True)
    assert state == "pending"
    assert reg.resolve(surface="email", address="john@acme.com", thread_id="t1") is None
    assert reg.approve(surface="email", address="john@acme.com", thread_id="t1") is True
    assert reg.resolve(surface="email", address="john@acme.com", thread_id="t1") is not None


def test_untrusted_provenance_is_always_pending(reg):
    # An outbound triggered downstream of untrusted content must NOT self-grant trust,
    # even if require_approval is False.
    state = reg.seed(surface="email", address="evil@bad.com", session_id="s1",
                     user_id="u_owner", thread_id="t1", provenance="untrusted",
                     require_approval=False)
    assert state == "pending"
    assert reg.resolve(surface="email", address="evil@bad.com", thread_id="t1") is None


def test_unknown_sender_never_resolves_thread_hijack(reg):
    # A real correspondent on thread t1...
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False)
    # ...an attacker forges a reply into t1 from a DIFFERENT address -> no binding.
    assert reg.resolve(surface="email", address="attacker@evil.com", thread_id="t1") is None


def test_address_only_resolves_when_single_active_binding(reg):
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False)
    row = reg.resolve(surface="email", address="john@acme.com")  # no thread_id
    assert row is not None and row["session_id"] == "s1"


def test_same_tenant_ambiguity_routes_to_latest_conversation(reg):
    """A4 (2026-07-13 review): same-tenant multi-session address-only replies route
    to the most recently active binding (default ON) — the legacy None starved
    every session after the first. Exact thread matches still win outright."""
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False, now=1000.0)
    reg.seed(surface="email", address="john@acme.com", session_id="s2",
             user_id="u_owner", thread_id="t2", provenance="owner",
             require_approval=False, now=2000.0)
    row = reg.resolve(surface="email", address="john@acme.com")
    assert row is not None and row["session_id"] == "s2"
    # an exact thread still resolves to ITS session, not the latest
    assert reg.resolve(surface="email", address="john@acme.com",
                       thread_id="t1")["session_id"] == "s1"


def test_same_tenant_ambiguity_denies_when_latest_fallback_off(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_RESOLVE_LATEST", "false")
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False, now=1000.0)
    reg.seed(surface="email", address="john@acme.com", session_id="s2",
             user_id="u_owner", thread_id="t2", provenance="owner",
             require_approval=False, now=2000.0)
    assert reg.resolve(surface="email", address="john@acme.com") is None


def test_cross_tenant_ambiguity_still_denies(reg):
    """Two TENANTS bound to one address can never be disambiguated by recency —
    routing a reply into the wrong tenant is a data leak. Stays None."""
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="tenant_a", thread_id="t1", provenance="owner",
             require_approval=False, now=1000.0)
    reg.seed(surface="email", address="john@acme.com", session_id="s2",
             user_id="tenant_b", thread_id="t2", provenance="owner",
             require_approval=False, now=2000.0)
    assert reg.resolve(surface="email", address="john@acme.com") is None


def test_expired_binding_stops_resolving(reg):
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner",
             require_approval=False, now=1000.0)
    # 31 days later, purge with a 30-day TTL
    purged = reg.purge_expired(ttl_secs=30 * 86400, now=1000.0 + 31 * 86400)
    assert purged >= 1
    assert reg.resolve(surface="email", address="john@acme.com", thread_id="t1") is None


def test_seed_count_for_cap_is_tenant_scoped(reg):
    reg.seed(surface="email", address="a@x.com", session_id="s1", user_id="u_owner",
             thread_id="t1", provenance="owner", require_approval=False, now=2000.0)
    reg.seed(surface="email", address="b@x.com", session_id="s1", user_id="u_owner",
             thread_id="t2", provenance="owner", require_approval=False, now=2001.0)
    reg.seed(surface="email", address="c@x.com", session_id="s9", user_id="u_other",
             thread_id="t3", provenance="owner", require_approval=False, now=2002.0)
    assert reg.count_seeds_since(user_id="u_owner", since_secs=86400, now=2100.0) == 2
    assert reg.count_seeds_since(user_id="u_other", since_secs=86400, now=2100.0) == 1


def test_cross_tenant_same_address_does_not_leak(reg):
    # Two tenants both email the SAME third party -> two rows (user_id in PK), and an
    # address-only resolve is ambiguous (2 active) -> None -> denied, NOT mis-routed to
    # the first tenant's session (Fusion: cross-tenant leak defense).
    reg.seed(surface="email", address="bob@x.com", session_id="sess_A", user_id="u_A",
             thread_id="", provenance="owner", require_approval=False)
    reg.seed(surface="email", address="bob@x.com", session_id="sess_B", user_id="u_B",
             thread_id="", provenance="owner", require_approval=False)
    assert reg.resolve(surface="email", address="bob@x.com") is None
    # both rows exist, distinct tenants
    assert {r["user_id"] for r in reg.list()} == {"u_A", "u_B"}


def test_seed_is_idempotent_on_key(reg):
    reg.seed(surface="email", address="john@acme.com", session_id="s1", user_id="u_owner",
             thread_id="t1", provenance="owner", require_approval=False)
    reg.seed(surface="email", address="john@acme.com", session_id="s1", user_id="u_owner",
             thread_id="t1", provenance="owner", require_approval=False)
    # one row, resolves once
    assert reg.resolve(surface="email", address="john@acme.com", thread_id="t1")["session_id"] == "s1"
    assert reg.count_seeds_since(user_id="u_owner", since_secs=10**9) == 1


def test_unscoped_approve_refuses_when_pending_rows_span_tenants(reg):
    """P1 (finalization): two tenants share (surface, address, thread) as pending.
    An unscoped approve() (user_id=None, the CLI default) must NOT cross-promote
    both — it refuses and returns False; the caller must pass user_id."""
    reg.seed(surface="email", address="x@acme.com", session_id="s1",
             user_id="tenant_a", thread_id="t", provenance="owner", require_approval=True)
    reg.seed(surface="email", address="x@acme.com", session_id="s2",
             user_id="tenant_b", thread_id="t", provenance="owner", require_approval=True)

    assert reg.approve(surface="email", address="x@acme.com", thread_id="t") is False
    # Neither tenant was promoted.
    assert reg.resolve(surface="email", address="x@acme.com", thread_id="t") is None


def test_scoped_approve_promotes_only_the_named_tenant(reg):
    reg.seed(surface="email", address="x@acme.com", session_id="s1",
             user_id="tenant_a", thread_id="t", provenance="owner", require_approval=True)
    reg.seed(surface="email", address="x@acme.com", session_id="s2",
             user_id="tenant_b", thread_id="t", provenance="owner", require_approval=True)

    assert reg.approve(surface="email", address="x@acme.com", thread_id="t",
                       user_id="tenant_a") is True
    row = reg.resolve(surface="email", address="x@acme.com", thread_id="t")
    assert row is not None and row["user_id"] == "tenant_a"


def test_unscoped_approve_still_works_for_single_tenant(reg):
    reg.seed(surface="email", address="solo@acme.com", session_id="s1",
             user_id="only_tenant", thread_id="t", provenance="owner", require_approval=True)
    assert reg.approve(surface="email", address="solo@acme.com", thread_id="t") is True


def test_ttl_expired_binding_can_be_reopened_by_a_new_seed(reg):
    """AC3: a TTL-idle binding must not be a permanent tombstone — the next
    outbound to that contact seeds it again (policy decides pending/active)."""
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", provenance="owner", require_approval=False,
             now=1000.0)
    assert reg.purge_expired(ttl_secs=30 * 86400, now=1000.0 + 31 * 86400) == 1
    state = reg.seed(surface="email", address="john@acme.com", session_id="s2",
                     user_id="u_owner", provenance="owner",
                     require_approval=False, now=1000.0 + 32 * 86400)
    assert state == "active"
    assert reg.resolve(surface="email", address="john@acme.com")["session_id"] == "s2"


def test_ttl_purge_keeps_an_owner_rejection_tombstone(reg):
    """AC3: an owner's reject stays a tombstone; the TTL never reopens it."""
    reg.seed(surface="email", address="spam@x.com", session_id="s1",
             user_id="u_owner", provenance="owner", require_approval=True,
             now=1000.0)
    assert reg.reject(surface="email", address="spam@x.com", user_id="u_owner",
                      now=1001.0)
    reg.purge_expired(ttl_secs=30 * 86400, now=1000.0 + 31 * 86400)
    assert reg.seed(surface="email", address="spam@x.com", session_id="s2",
                    user_id="u_owner", provenance="owner",
                    require_approval=True) == "expired"


def test_seed_race_does_not_raise_integrity_error(reg, monkeypatch):
    """AC4: two seeds race SELECT-then-INSERT. The loser must read the
    winner's row back, never raise IntegrityError (swallowed as `disabled`)."""
    import core.surfaces.correspondents as mod
    reg.seed(surface="email", address="j@x.com", session_id="s1",
             user_id="u_owner", provenance="owner", require_approval=False)
    real = mod.execute_retry
    calls = {"n": 0}

    def racing(db, sql, params=(), fetch=None, **kw):
        # The loser's existence probe ran before the winner's INSERT.
        if sql.lstrip().startswith("SELECT state") and calls["n"] == 0:
            calls["n"] += 1
            return None
        return real(db, sql, params, fetch=fetch, **kw)

    monkeypatch.setattr(mod, "execute_retry", racing)
    assert reg.seed(surface="email", address="j@x.com", session_id="s1",
                    user_id="u_owner", provenance="owner",
                    require_approval=True) == "active"


def test_thread_anchor_race_does_not_raise(reg, monkeypatch):
    import core.surfaces.correspondents as mod
    reg.seed(surface="email", address="j@x.com", session_id="s1",
             user_id="u_owner", provenance="owner", require_approval=False)
    reg.seed_thread_anchor(surface="email", address="j@x.com", thread_id="<m1>",
                           session_id="s1", user_id="u_owner")
    real = mod.execute_retry
    calls = {"n": 0}

    def racing(db, sql, params=(), fetch=None, **kw):
        if "thread_id=? AND user_id=?" in sql and sql.lstrip().startswith(
                "SELECT state") and calls["n"] == 0:
            calls["n"] += 1
            return None
        return real(db, sql, params, fetch=fetch, **kw)

    monkeypatch.setattr(mod, "execute_retry", racing)
    assert reg.seed_thread_anchor(surface="email", address="j@x.com",
                                  thread_id="<m1>", session_id="s1",
                                  user_id="u_owner") == "active"


def test_prune_thread_anchors_drops_only_old_anchor_rows(reg):
    """AC5: one anchor row per outbound message grew without bound."""
    reg.seed(surface="email", address="j@x.com", session_id="s1",
             user_id="u_owner", provenance="owner", require_approval=False,
             now=1000.0)
    reg.seed_thread_anchor(surface="email", address="j@x.com", thread_id="<old>",
                           session_id="s1", user_id="u_owner", now=1000.0)
    reg.seed_thread_anchor(surface="email", address="j@x.com", thread_id="<new>",
                           session_id="s1", user_id="u_owner", now=90 * 86400.0)
    removed = reg.prune_thread_anchors(max_age_secs=30 * 86400,
                                       now=91 * 86400.0)
    assert removed == 1
    tids = {r["thread_id"] for r in reg.list()}
    assert "<old>" not in tids and "<new>" in tids
    # The base binding is NOT an anchor and is never pruned here.
    assert "" in tids
