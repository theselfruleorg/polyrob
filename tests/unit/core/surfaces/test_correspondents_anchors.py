"""098 CO-12: thread anchors hold no trust of their own — every read derives it
from the base (address-level) binding. One approve clears the inbox copies, a
reply to an anchor routes to its sending session, and a purged or revoked base
takes its anchors with it."""
import sqlite3

import pytest

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.owner_admin import pending_correspondent_items


@pytest.fixture
def reg(tmp_path):
    return CorrespondentRegistry(str(tmp_path / "correspondents.db"))


def _seed_two_emails(reg):
    reg.seed(surface="email", address="bob@x.com", session_id="s1", user_id="u1")
    reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m1>",
                           session_id="s1", user_id="u1")
    reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m2>",
                           session_id="s2", user_id="u1")


def test_single_approve_clears_inbox_items(reg):
    _seed_two_emails(reg)
    assert reg.approve(surface="email", address="bob@x.com", user_id="u1")
    assert pending_correspondent_items(reg, "u1") == []


def test_reply_to_anchor_routes_to_sending_session_after_approve(reg):
    _seed_two_emails(reg)
    assert reg.resolve(surface="email", address="bob@x.com", thread_id="<m2>") is None
    reg.approve(surface="email", address="bob@x.com", user_id="u1")
    row = reg.resolve(surface="email", address="bob@x.com", thread_id="<m2>")
    assert row and row["session_id"] == "s2" and row["state"] == "active"


def test_ttl_purge_stops_anchor_routing(reg):
    _seed_two_emails(reg)
    reg.approve(surface="email", address="bob@x.com", user_id="u1", now=100.0)
    # The anchors are fresher than the idle base row: only the base is stale.
    reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m3>",
                           session_id="s3", user_id="u1", now=10_000.0)
    with sqlite3.connect(reg.db_path) as c:
        c.execute("UPDATE correspondents SET updated_at=100 WHERE provenance != 'thread'")
        c.execute("UPDATE correspondents SET updated_at=10000 WHERE provenance = 'thread'")
    reg.purge_expired(1000, now=5000.0)
    assert reg.resolve(surface="email", address="bob@x.com", thread_id="<m3>") is None
    assert reg.resolve(surface="email", address="bob@x.com") is None
    assert reg.list("u1") == []


def test_anchor_without_base_never_resolves(reg):
    reg.seed(surface="email", address="bob@x.com", session_id="s1", user_id="u1",
             require_approval=False)
    reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m1>",
                           session_id="s1", user_id="u1")
    with sqlite3.connect(reg.db_path) as c:
        c.execute("DELETE FROM correspondents WHERE provenance != 'thread'")
    assert reg.resolve(surface="email", address="bob@x.com", thread_id="<m1>") is None
    assert reg.resolve(surface="email", address="bob@x.com") is None


def test_address_scoped_deactivate_expires_anchors(reg):
    reg.seed(surface="email", address="bob@x.com", session_id="s1", user_id="u1",
             require_approval=False)
    reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m1>",
                           session_id="s1", user_id="u1")
    assert reg.deactivate(surface="email", address="bob@x.com", user_id="u1")
    assert {r["state"] for r in reg.list("u1")} == {"expired"}
    assert reg.resolve(surface="email", address="bob@x.com", thread_id="<m1>") is None


def test_migration_v2_aligns_anchor_state(tmp_path):
    path = str(tmp_path / "correspondents.db")
    reg = CorrespondentRegistry(path)
    _seed_two_emails(reg)
    reg.seed(surface="email", address="eve@x.com", session_id="s9", user_id="u1")
    reg.seed_thread_anchor(surface="email", address="eve@x.com", thread_id="<e1>",
                           session_id="s9", user_id="u1")
    with sqlite3.connect(path) as c:
        # A pre-098 file: the base was approved, the anchors kept "pending"; an
        # orphan anchor whose base row is gone.
        c.execute("UPDATE correspondents SET state='active' WHERE address='bob@x.com' "
                  "AND provenance != 'thread'")
        c.execute("DELETE FROM correspondents WHERE address='eve@x.com' "
                  "AND provenance != 'thread'")
        c.execute("PRAGMA user_version = 1")
    reg2 = CorrespondentRegistry(path)
    rows = {(r["address"], r["thread_id"]): r["state"] for r in reg2.list("u1")}
    assert rows == {("bob@x.com", ""): "active", ("bob@x.com", "<m1>"): "active",
                    ("bob@x.com", "<m2>"): "active"}
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_approve_and_anchor_seed_concurrent(reg, monkeypatch):
    """An anchor seeded while the base flips (its read saw ``pending``) still
    lands with the base's CURRENT state — the insert reads it."""
    reg.seed(surface="email", address="bob@x.com", session_id="s1", user_id="u1")
    import core.surfaces.correspondents as mod
    real = mod.execute_retry
    fired = {}

    def racing(db, sql, params=(), **kw):
        if "INSERT INTO correspondents" in sql and "'thread'" in sql and not fired:
            fired["x"] = True
            reg.approve(surface="email", address="bob@x.com", user_id="u1")
        return real(db, sql, params, **kw)

    monkeypatch.setattr(mod, "execute_retry", racing)
    state = reg.seed_thread_anchor(surface="email", address="bob@x.com", thread_id="<m1>",
                                   session_id="s1", user_id="u1")
    monkeypatch.setattr(mod, "execute_retry", real)
    assert fired and state == "active"
    assert pending_correspondent_items(reg, "u1") == []
    row = reg.resolve(surface="email", address="bob@x.com", thread_id="<m1>")
    assert row and row["session_id"] == "s1"
