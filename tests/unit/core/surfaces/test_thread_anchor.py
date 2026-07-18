"""A3 (2026-07-13 correspondent review): registry thread-anchor rows.

Seeding stored thread_id='' while inbound replies resolve with the In-Reply-To
Message-ID, so resolve()'s exact-thread branch never fired for email. Thread
anchors bind each outbound Message-ID to its sending session:
- provenance='thread', state mirrors the base (address-level) row;
- no base row -> no anchor (an anchor is never a trust bootstrap);
- excluded from the per-day NEW-correspondent cap (structural rows).
"""
import os
import tempfile

from core.surfaces.correspondents import CorrespondentRegistry


def _reg():
    return CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))


def test_anchor_inherits_active_state_and_resolves_exactly():
    reg = _reg()
    reg.seed(surface="email", address="john@acme.com", session_id="sess-1",
             user_id="t1", require_approval=False)
    state = reg.seed_thread_anchor(surface="email", address="john@acme.com",
                                   thread_id="<mid-1@rob>", session_id="sess-1",
                                   user_id="t1")
    assert state == "active"
    row = reg.resolve(surface="email", address="john@acme.com",
                      thread_id="<mid-1@rob>")
    assert row is not None and row["session_id"] == "sess-1"


def test_anchor_disambiguates_two_sessions_to_same_address():
    """The point of the exercise: session-2's thread gets session-2's replies."""
    reg = _reg()
    reg.seed(surface="email", address="john@acme.com", session_id="sess-1",
             user_id="t1", require_approval=False)
    reg.seed_thread_anchor(surface="email", address="john@acme.com",
                           thread_id="<mid-1@rob>", session_id="sess-1", user_id="t1")
    reg.seed_thread_anchor(surface="email", address="john@acme.com",
                           thread_id="<mid-2@rob>", session_id="sess-2", user_id="t1")
    r1 = reg.resolve(surface="email", address="john@acme.com", thread_id="<mid-1@rob>")
    r2 = reg.resolve(surface="email", address="john@acme.com", thread_id="<mid-2@rob>")
    assert r1["session_id"] == "sess-1"
    assert r2["session_id"] == "sess-2"


def test_anchor_without_base_row_is_refused():
    reg = _reg()
    state = reg.seed_thread_anchor(surface="email", address="stranger@x.com",
                                   thread_id="<mid@rob>", session_id="s",
                                   user_id="t1")
    assert state is None
    assert reg.resolve(surface="email", address="stranger@x.com",
                       thread_id="<mid@rob>") is None


def test_anchor_inherits_pending_state_not_routable():
    reg = _reg()
    reg.seed(surface="email", address="john@acme.com", session_id="sess-1",
             user_id="t1", require_approval=True)  # pending
    state = reg.seed_thread_anchor(surface="email", address="john@acme.com",
                                   thread_id="<mid-1@rob>", session_id="sess-1",
                                   user_id="t1")
    assert state == "pending"
    assert reg.resolve(surface="email", address="john@acme.com",
                       thread_id="<mid-1@rob>") is None


def test_thread_rows_do_not_consume_the_daily_cap():
    reg = _reg()
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="t1", require_approval=False)
    for i in range(5):
        reg.seed_thread_anchor(surface="email", address="john@acme.com",
                               thread_id=f"<mid-{i}@rob>", session_id="s1",
                               user_id="t1")
    assert reg.count_seeds_since(user_id="t1", since_secs=86400) == 1, (
        "structural thread anchors must not eat the NEW-correspondent cap")


def test_anchor_is_idempotent():
    reg = _reg()
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="t1", require_approval=False)
    s1 = reg.seed_thread_anchor(surface="email", address="john@acme.com",
                                thread_id="<mid-1@rob>", session_id="s1", user_id="t1")
    s2 = reg.seed_thread_anchor(surface="email", address="john@acme.com",
                                thread_id="<mid-1@rob>", session_id="s1", user_id="t1")
    assert s1 == s2 == "active"
