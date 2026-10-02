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


def test_allowlist_matches_any_spelling_of_the_target(tmp_path):
    """OB13: the allowlist compared the raw target, so Bob@Corp.io != bob@corp.io."""
    import os
    import sqlite3
    import time
    from core.surfaces.outbound_allowlist import OutboundAllowlist
    db = os.path.join(str(tmp_path), "surfaces.db")
    al = OutboundAllowlist(db)
    al.allow("u", "email", "Bob@Corp.io")
    assert al.is_allowed("u", "email", "bob@corp.io")
    assert al.is_allowed("u", "email", " BOB@corp.IO ")
    al.allow("u", "telegram", "@SomeChannel")
    assert al.is_allowed("u", "telegram", "somechannel")
    assert al.is_allowed("u", "telegram", "t.me/SomeChannel")
    # a LEGACY raw-spelled row is matched and revoked too
    c = sqlite3.connect(db)
    c.execute("INSERT INTO outbound_allowlist VALUES ('u','email','Old@Corp.io','', 'active', ?)",
              (time.time(),))
    c.commit(); c.close()
    assert al.is_allowed("u", "email", "old@corp.io")
    assert al.revoke("u", "email", "OLD@corp.io") is True
    assert not al.is_allowed("u", "email", "Old@Corp.io")
