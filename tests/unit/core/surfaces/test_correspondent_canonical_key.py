"""The registry and the dead-target store key on the SAME canonical spelling the
conversation store uses, and a pre-existing file is collapsed once on open."""
import sqlite3
import time

from core.surfaces.correspondents import STATE_ACTIVE, STATE_PENDING, CorrespondentRegistry
from core.surfaces.dead_targets import DeadTargetStore


def test_at_handle_and_bare_handle_are_one_binding(tmp_path):
    reg = CorrespondentRegistry(str(tmp_path / "c.db"))
    reg.seed(surface="telegram", address="@ThePublicDen", session_id="s1",
             user_id="rob", require_approval=False)
    assert reg.seed(surface="telegram", address="t.me/thepublicden", session_id="s2",
                    user_id="rob", require_approval=False) == STATE_ACTIVE
    rows = reg.list(user_id="rob")
    assert [r["address"] for r in rows] == ["thepublicden"]


def _legacy_registry(path):
    """A registry file written by the lowercase-only rule: two spellings, one of
    them the owner-approved one."""
    CorrespondentRegistry(str(path))  # schema (fresh file: user_version bumps)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")
    now = time.time()
    conn.execute("INSERT INTO correspondents VALUES (?,?,?,?,?,?,?,?,?)",
                 ("telegram", "@thepublicden", "", "s-old", "rob", STATE_ACTIVE, "owner",
                  now - 100, now - 100))
    conn.execute("INSERT INTO correspondents VALUES (?,?,?,?,?,?,?,?,?)",
                 ("telegram", "thepublicden", "", "s-new", "rob", STATE_PENDING, "owner",
                  now, now))
    conn.commit(); conn.close()


def test_legacy_spellings_collapse_and_the_approved_binding_wins(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_registry(path)
    reg = CorrespondentRegistry(str(path))
    rows = reg.list(user_id="rob")
    assert len(rows) == 1
    assert rows[0]["address"] == "thepublicden"
    assert rows[0]["state"] == STATE_ACTIVE and rows[0]["session_id"] == "s-old"
    # idempotent: reopening does not touch it again
    CorrespondentRegistry(str(path))
    assert len(reg.list(user_id="rob")) == 1


def test_dead_target_marked_as_at_handle_is_found_bare(tmp_path):
    store = DeadTargetStore(str(tmp_path / "d.db"))
    store.mark("telegram", "@thepublicden", reason="blocked")
    assert store.is_dead("telegram", "thepublicden")
