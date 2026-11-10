"""098 CL-09: the direct `update --apply` path writes a phase journal; a kill
mid-way is rolled back by the next `polyrob update` or start."""
import json
import sqlite3

from cli.update.context import UpdateContext
from cli.update.engine import UpdateRunners, apply_update
from cli.update.journal import (JOURNAL_NAME, failed_rollback, recover_open_update,
                                update_journal)
from core.state.proc import ProcIdentity, process_identity


def _ctx(tmp_path):
    db = tmp_path / "home" / "bot.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE t (id INTEGER)")
    c.commit()
    c.close()
    return UpdateContext(data_home=tmp_path / "home", snapshots_root=tmp_path / "snaps",
                         db_paths=[db])


def test_journal_records_old_sha_before_install(tmp_path):
    ctx = _ctx(tmp_path)
    seen = {}

    def install():
        seen.update(json.loads((ctx.snapshots_root / JOURNAL_NAME).read_text()))

    runners = UpdateRunners(install=install, migrate=lambda: None, verify=lambda: None,
                            rollback_code=lambda: None,
                            prepare=lambda: {"old_sha": "abc123", "old_branch": "main"})
    res = apply_update(ctx=ctx, runners=runners, from_version="1", to_version="2",
                       journal=update_journal(ctx.snapshots_root))
    assert res.ok
    assert seen["phase"] == "install" and seen["info"]["old_sha"] == "abc123"
    assert seen["info"]["snapshot"]
    j = update_journal(ctx.snapshots_root).read()
    assert j["state"] == "done" and j["phase"] == "applied"


def _open_journal(root, owner):
    j = update_journal(root)
    j.begin("update", owner=owner.encode(), rollback_info={"started_at": 1.0})
    j.phase("prepared", snapshot="snap-1", old_sha="abc", old_branch="main")
    j.phase("install")
    return j


def test_recover_rolls_back_dead_owner(tmp_path):
    _open_journal(tmp_path, ProcIdentity(999999, "gone", "other-boot"))
    calls = []
    out = recover_open_update(tmp_path, rollback=lambda r: calls.append(r) or {"ok": True},
                              echo=lambda *_: None)
    assert out == "rolled_back"
    assert calls[0]["old_sha"] == "abc" and calls[0]["snapshot"] == "snap-1"
    assert update_journal(tmp_path).pending() is None


def test_live_owner_not_recovered(tmp_path):
    _open_journal(tmp_path, process_identity())
    calls = []
    out = recover_open_update(tmp_path, rollback=lambda r: calls.append(r) or {"ok": True},
                              echo=lambda *_: None)
    assert out == "live" and calls == []


def test_rollback_failed_blocks_next_apply(tmp_path):
    _open_journal(tmp_path, ProcIdentity(999999, "gone", "other-boot"))
    out = recover_open_update(tmp_path, rollback=lambda r: {"ok": False, "errors": ["code: x"]},
                              echo=lambda *_: None)
    assert out == "rollback_failed"
    assert failed_rollback(tmp_path) is not None


def test_in_process_rollback_failure_is_journaled(tmp_path):
    ctx = _ctx(tmp_path)

    def boom():
        raise RuntimeError("pip died")

    def bad_rollback():
        raise RuntimeError("git reset refused")

    runners = UpdateRunners(install=boom, migrate=lambda: None, verify=lambda: None,
                            rollback_code=bad_rollback)
    res = apply_update(ctx=ctx, runners=runners, from_version="1",
                       journal=update_journal(ctx.snapshots_root))
    assert not res.ok and res.rollback_errors
    assert failed_rollback(ctx.snapshots_root) is not None


def test_nothing_mutated_yet_is_just_closed(tmp_path):
    j = update_journal(tmp_path)
    j.begin("update", owner=ProcIdentity(999999, "gone", "x").encode())
    j.phase("snapshot")
    calls = []
    assert recover_open_update(tmp_path, rollback=lambda r: calls.append(r),
                               echo=lambda *_: None) == "rolled_back"
    assert calls == []


def test_two_recoverers_never_both_roll_back(tmp_path, monkeypatch):
    """ST-8: two starts that both saw the dead owner both rolled back. The first
    now claims the entry under the journal lock; the second reads it as live."""
    j = _open_journal(tmp_path, ProcIdentity(999999, "gone", "other-boot"))
    real_claim = j.claim.__func__
    raced = {}

    def _claim_after_a_rival(self, expect, new):
        if not raced:  # a rival process takes the entry between our read and claim
            raced["rival"] = real_claim(self, expect, "rival-owner")
        return real_claim(self, expect, new)

    monkeypatch.setattr(type(j), "claim", _claim_after_a_rival)
    calls = []
    out = recover_open_update(tmp_path, rollback=lambda r: calls.append(r) or {"ok": True},
                              echo=lambda *_: None)
    assert raced["rival"] is not None
    assert out == "live" and calls == []
