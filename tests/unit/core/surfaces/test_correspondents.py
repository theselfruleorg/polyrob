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


def test_ambiguous_address_requires_thread(reg):
    # Same correspondent, two active sessions -> address-only is ambiguous -> None.
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner", require_approval=False)
    reg.seed(surface="email", address="john@acme.com", session_id="s2",
             user_id="u_owner", thread_id="t2", provenance="owner", require_approval=False)
    assert reg.resolve(surface="email", address="john@acme.com") is None
    # but an exact thread still resolves
    assert reg.resolve(surface="email", address="john@acme.com", thread_id="t2")["session_id"] == "s2"


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
