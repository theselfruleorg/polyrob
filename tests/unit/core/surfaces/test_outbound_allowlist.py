import os, tempfile
from core.surfaces.outbound_allowlist import OutboundAllowlist

def _store():
    d = tempfile.mkdtemp()
    return OutboundAllowlist(os.path.join(d, "allow.db"))

def test_default_deny():
    s = _store()
    assert s.is_allowed("u1", "telegram", "12345") is False

def test_allow_then_check():
    s = _store()
    s.allow("u1", "telegram", "12345", note="team group")
    assert s.is_allowed("u1", "telegram", "12345") is True

def test_tenant_scoped():
    s = _store()
    s.allow("u1", "telegram", "12345")
    assert s.is_allowed("u2", "telegram", "12345") is False  # other tenant denied

def test_revoke():
    s = _store()
    s.allow("u1", "telegram", "12345")
    assert s.revoke("u1", "telegram", "12345") is True
    assert s.is_allowed("u1", "telegram", "12345") is False

def test_list():
    s = _store()
    s.allow("u1", "telegram", "12345", note="g")
    rows = s.list("u1")
    assert len(rows) == 1 and rows[0]["target"] == "12345" and rows[0]["status"] == "active"

def test_allow_idempotent():
    s = _store()
    s.allow("u1", "telegram", "12345")
    s.allow("u1", "telegram", "12345", note="updated")
    assert len(s.list("u1")) == 1
