"""E1/E2/E3/E6 (2026-07-13 review): ConversationStore — the durable, address-keyed
conversation container sessions come and go around."""
import os
import tempfile

from core.surfaces.conversations import ConversationStore


def _store():
    return ConversationStore(os.path.join(tempfile.mkdtemp(), "conversations.db"))


def test_record_roundtrip_and_history_order():
    s = _store()
    s.record_outbound("t1", "email", "John@Acme.com", "hello john",
                      mid="<m1@rob>", subject="Intro", session_id="s1", now=1000.0)
    s.record_inbound("t1", "email", "john@acme.com", "hi rob",
                     mid="<r1@acme>", now=2000.0)
    conv = s.get("t1", "email", "john@acme.com")
    assert conv is not None
    assert conv["session_id"] == "s1"
    assert conv["last_inbound_mid"] == "<r1@acme>"
    assert conv["last_inbound_ts"] == 2000.0
    assert conv["last_outbound_ts"] == 1000.0
    hist = s.history("t1", "email", "john@acme.com")
    assert [m["direction"] for m in hist] == ["out", "in"]
    assert hist[0]["body"] == "hello john"


def test_tenant_scoping():
    s = _store()
    s.record_outbound("t1", "email", "j@x.com", "from t1", session_id="s1")
    s.record_outbound("t2", "email", "j@x.com", "from t2", session_id="s2")
    assert s.get("t1", "email", "j@x.com")["session_id"] == "s1"
    assert s.get("t2", "email", "j@x.com")["session_id"] == "s2"
    assert len(s.list("t1")) == 1


def test_body_cap_and_prune():
    s = _store()
    s.record_outbound("t1", "email", "j@x.com", "x" * 5000, now=1.0)
    hist = s.history("t1", "email", "j@x.com")
    assert len(hist[0]["body"]) == 2000
    for i in range(210):
        s.record_inbound("t1", "email", "j@x.com", f"m{i}", now=10.0 + i)
    hist = s.history("t1", "email", "j@x.com", limit=500)
    assert len(hist) == 200, "conversation log must stay bounded"
    assert hist[-1]["body"] == "m209"


def test_format_context_contains_both_directions_and_header():
    s = _store()
    s.record_outbound("t1", "email", "j@x.com", "our offer", now=1000.0,
                      session_id="s1", subject="Offer")
    s.record_inbound("t1", "email", "j@x.com", "their counter", now=2000.0)
    ctx = s.format_context("t1", "email", "j@x.com")
    assert "conversation with email:j@x.com" in ctx
    assert "we sent" in ctx and "our offer" in ctx
    assert "they wrote" in ctx and "their counter" in ctx


def test_format_context_empty_when_unknown():
    s = _store()
    assert s.format_context("t1", "email", "nobody@x.com") == ""


def test_format_context_respects_max_chars():
    s = _store()
    for i in range(20):
        s.record_inbound("t1", "email", "j@x.com", "long body " * 50, now=float(i))
    ctx = s.format_context("t1", "email", "j@x.com", limit=20, max_chars=500)
    assert len(ctx) <= 500


def test_rebind_session():
    s = _store()
    s.record_outbound("t1", "email", "j@x.com", "hello", session_id="dead-sess")
    s.rebind_session("t1", "email", "j@x.com", "new-sess")
    assert s.get("t1", "email", "j@x.com")["session_id"] == "new-sess"


def test_outbound_count_since():
    s = _store()
    s.record_outbound("t1", "email", "j@x.com", "one", now=1000.0)
    s.record_outbound("t1", "email", "j@x.com", "two", now=2000.0)
    s.record_inbound("t1", "email", "j@x.com", "reply", now=2500.0)
    assert s.outbound_count_since("t1", "email", "j@x.com", 5000, now=3000.0) == 2
    assert s.outbound_count_since("t1", "email", "j@x.com", 500, now=3000.0) == 0
    assert s.outbound_count_since("t1", "email", "nobody@x.com", 5000) == 0


def test_format_list_shows_replied_state_at_a_glance():
    """E4 (slimmed): 'who did we contact, who replied' straight from the store —
    no campaign subsystem needed."""
    s = _store()
    s.record_outbound("t1", "email", "a@x.com", "hi a", now=1000.0, session_id="s1")
    s.record_outbound("t1", "email", "b@x.com", "hi b", now=1100.0, session_id="s1")
    s.record_inbound("t1", "email", "b@x.com", "hello!", now=1200.0)
    out = s.format_list("t1")
    assert "email:a@x.com" in out and "email:b@x.com" in out
    a_line = next(l for l in out.splitlines() if "a@x.com" in l)
    b_line = next(l for l in out.splitlines() if "b@x.com" in l)
    assert "no reply yet" in a_line
    assert "no reply yet" not in b_line


def test_format_list_empty_tenant():
    s = _store()
    assert s.format_list("t-nobody") == ""
