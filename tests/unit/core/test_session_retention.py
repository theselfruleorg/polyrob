"""WS-K2 — session-tree retention: plan then apply, and four ways to be kept.

Prod on 2026-09-22: 2,449 trees, 5.0 GB, 694 older than 30 days, no policy.
These tests pin the four protections and the two structural guards, because
this is the only module in the knowledge work that DELETES anything.
"""
import os
import sqlite3
import time
import uuid

import pytest

from core.session_retention import (
    KEEP_ARTIFACT, KEEP_CAP, KEEP_GOAL, KEEP_RECENT, KEEP_REGISTERED, KEEP_SYMLINK,
    apply_sweep, plan_sweep, sweep,
)

NOW = 1_800_000_000.0
DAY = 86400.0


def _sid(label):
    """A stable UUID per label: only UUID-named trees are session trees."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


def _tree(root, tenant, name, *, age_days, files=("workspace/out.md",)):
    d = root / tenant / _sid(name)
    for rel in files:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    stamp = NOW - age_days * DAY
    for dirpath, dirnames, filenames in os.walk(d):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (stamp, stamp))
    os.utime(d, (stamp, stamp))
    return d


def _goals_db(home, rows):
    con = sqlite3.connect(os.path.join(home, "goals.db"))
    con.execute("CREATE TABLE goals (id TEXT, user_id TEXT, status TEXT, "
                "session_id TEXT, payload TEXT)")
    for gid, status, sid in rows:
        con.execute("INSERT INTO goals VALUES (?,?,?,?,?)",
                    (gid, "rob", status, sid, "{}"))
    con.commit()
    con.close()


def _goals_db_with_payload(home, rows):
    con = sqlite3.connect(os.path.join(home, "goals.db"))
    con.execute("CREATE TABLE goals (id TEXT, user_id TEXT, status TEXT, "
                "session_id TEXT, payload TEXT)")
    for gid, status, sid, payload in rows:
        con.execute("INSERT INTO goals VALUES (?,?,?,?,?)",
                    (gid, "rob", status, sid, payload))
    con.commit()
    con.close()


def _artifacts_db(home, paths):
    con = sqlite3.connect(os.path.join(home, "artifacts.db"))
    con.execute("CREATE TABLE artifacts (id TEXT, user_id TEXT, path TEXT)")
    for i, p in enumerate(paths):
        con.execute("INSERT INTO artifacts VALUES (?,?,?)", (str(i), "rob", str(p)))
    con.commit()
    con.close()


def _registry_db(home, session_ids):
    con = sqlite3.connect(os.path.join(home, "session_registry.db"))
    con.execute("CREATE TABLE active_sessions (session_id TEXT PRIMARY KEY, "
                "worker_pid INTEGER, status TEXT, params TEXT, last_seen_at TEXT, "
                "created_at TEXT, owner_boot_id TEXT)")
    for sid in session_ids:
        con.execute("INSERT INTO active_sessions VALUES (?,?,?,?,?,?,?)",
                    (sid, 1, "active", "{}", "t", "t", "b"))
    con.commit()
    con.close()


@pytest.fixture
def home(tmp_path):
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _plan(home, **kw):
    return plan_sweep(str(home), now=NOW,
                      sessions_root=str(home / "sessions"), **kw)


def test_old_trees_go_and_recent_ones_stay(home):
    old = _tree(home / "sessions", "rob", "old-one", age_days=200)
    new = _tree(home / "sessions", "rob", "new-one", age_days=2)

    plan = _plan(home, days=90)

    assert plan.remove == [str(old)]
    assert plan.keep[str(new)] == KEEP_RECENT


def test_a_tree_holding_a_registered_artifact_is_never_deleted(home):
    keeper = _tree(home / "sessions", "rob", "has-evidence", age_days=300)
    _tree(home / "sessions", "rob", "empty-old", age_days=300)
    _artifacts_db(home, [keeper / "workspace" / "out.md"])

    plan = _plan(home, days=90)

    assert plan.keep[str(keeper)] == KEEP_ARTIFACT
    assert str(keeper) not in plan.remove


def test_an_unknown_goal_status_protects_rather_than_releases(home):
    """⚠️ The board holds objectives and asks too, each with its own vocabulary
    (`open`/`fulfilled`/`rejected`/`obsolete` are all live on prod). An
    allow-list of "live" statuses would treat every word it had not heard of as
    safe to delete, so the rule is a DENY list of terminal statuses."""
    weird = _tree(home / "sessions", "rob", "odd-status", age_days=300)
    _goals_db(home, [("g1", "fulfilled", _sid("odd-status"))])

    plan = _plan(home, days=90)

    assert plan.keep[str(weird)] == KEEP_GOAL
    assert plan.remove == []


def test_a_session_named_only_in_the_payload_is_protected(home):
    """Older rows carry the session in the payload, not the column."""
    named = _tree(home / "sessions", "rob", "payload-session", age_days=300)
    _goals_db_with_payload(home, [
        ("g1", "ready", "", '{"origin_session_id": "%s"}' % _sid("payload-session"))])

    plan = _plan(home, days=90)

    assert plan.keep[str(named)] == KEEP_GOAL


def test_an_unparseable_payload_does_not_refuse_the_sweep(home):
    _tree(home / "sessions", "rob", "old-one", age_days=300)
    _goals_db_with_payload(home, [("g1", "ready", "", "{not json")])

    plan = _plan(home, days=90)

    assert len(plan.remove) == 1


def test_a_live_goal_keeps_its_session(home):
    named = _tree(home / "sessions", "rob", "goal-session", age_days=300)
    done = _tree(home / "sessions", "rob", "done-session", age_days=300)
    _goals_db(home, [("g1", "running", _sid("goal-session")), ("g2", "done", _sid("done-session"))])

    plan = _plan(home, days=90)

    assert plan.keep[str(named)] == KEEP_GOAL
    assert plan.remove == [str(done)]


def test_a_worker_that_still_owns_a_session_keeps_it(home):
    owned = _tree(home / "sessions", "rob", "live-session", age_days=300)
    _registry_db(home, [_sid("live-session")])

    plan = _plan(home, days=90)

    assert plan.keep[str(owned)] == KEEP_REGISTERED
    assert plan.remove == []


def test_an_unreadable_goal_board_refuses_the_whole_sweep(home):
    _tree(home / "sessions", "rob", "old-one", age_days=300)
    with open(os.path.join(home, "goals.db"), "wb") as fh:
        fh.write(b"not a database")

    with pytest.raises(RuntimeError, match="goals.db unreadable"):
        _plan(home, days=90)


def test_an_unreadable_artifact_registry_refuses_the_whole_sweep(home):
    _tree(home / "sessions", "rob", "old-one", age_days=300)
    with open(os.path.join(home, "artifacts.db"), "wb") as fh:
        fh.write(b"not a database")

    with pytest.raises(RuntimeError, match="artifacts.db unreadable"):
        _plan(home, days=90)


def test_a_symlink_is_never_followed_and_never_removed(home, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete me")
    link = home / "sessions" / "rob" / _sid("a-link")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    os.utime(link, (NOW - 300 * DAY, NOW - 300 * DAY), follow_symlinks=False)

    plan = _plan(home, days=90)

    assert plan.keep[str(link)] == KEEP_SYMLINK
    assert plan.remove == []
    assert (outside / "keep.txt").exists()


def test_apply_refuses_a_path_outside_the_root(home):
    _tree(home / "sessions", "rob", "old-one", age_days=300)
    plan = _plan(home, days=90)
    plan.remove.append(str(home / "elsewhere"))       # a caller-injected path

    out = apply_sweep(plan)

    assert out["removed"] == 1 and out["failed"] == 1
    assert any("refused" in e for e in plan.errors)


def test_the_cap_drains_a_backlog_over_several_runs(home):
    for i in range(5):
        _tree(home / "sessions", "rob", f"old-{i}", age_days=300)

    plan = _plan(home, days=90, max_removals=2)

    assert len(plan.remove) == 2
    assert plan.kept_for(KEEP_CAP) == 3


def test_retention_disabled_removes_nothing(home):
    _tree(home / "sessions", "rob", "old-one", age_days=300)
    plan = _plan(home, days=0)
    assert plan.remove == [] and plan.errors


def test_sweep_deletes_what_the_plan_named(home, monkeypatch):
    old = _tree(home / "sessions", "rob", "old-one", age_days=300)
    keep = _tree(home / "sessions", "rob", "new-one", age_days=1)
    monkeypatch.setenv("DATA_ROOT", str(home / "sessions"))
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")

    out = sweep(str(home), now=NOW)

    assert out["removed"] == 1
    assert not old.exists() and keep.exists()


def test_sweep_is_a_noop_when_nothing_is_old(home, monkeypatch):
    _tree(home / "sessions", "rob", "new-one", age_days=1)
    monkeypatch.setenv("DATA_ROOT", str(home / "sessions"))
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "90")

    assert sweep(str(home), now=NOW) == {"removed": 0, "failed": 0, "scanned": 1}


# --- H13 (2026-09-23): the sweep is rooted, UUID-only, and ages by the whole tree ---

def test_a_root_outside_the_data_home_is_refused(home, tmp_path_factory):
    """In the local CLI the old fallback was ``./data/task`` — the user's project."""
    project = tmp_path_factory.mktemp("project")
    victim = _tree(project / "data" / "task", "rob", "old-one", age_days=300)

    plan = plan_sweep(str(home), now=NOW, days=90,
                      sessions_root=str(project / "data" / "task"))

    assert plan.remove == []
    assert any("not inside the data home" in e for e in plan.errors)
    assert victim.exists()


def test_the_data_home_itself_is_not_a_session_root(home):
    plan = plan_sweep(str(home), now=NOW, days=90, sessions_root=str(home))
    assert plan.remove == [] and any("refused" in e for e in plan.errors)


def test_default_root_is_the_data_home_sessions_dir_not_cwd(home, monkeypatch, tmp_path_factory):
    cwd = tmp_path_factory.mktemp("cwd")
    decoy = _tree(cwd / "data" / "task", "rob", "decoy", age_days=300)
    old = _tree(home / "sessions", "rob", "old-one", age_days=300)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)

    plan = plan_sweep(str(home), now=NOW, days=90)

    assert plan.root == str(home / "sessions")
    assert plan.remove == [str(old)]
    assert decoy.exists()


def test_only_uuid_named_trees_are_candidates(home):
    for name in ("sessions", "workspace", "local", "not-a-uuid"):
        d = home / "sessions" / "rob" / name
        d.mkdir(parents=True)
        os.utime(d, (NOW - 300 * DAY, NOW - 300 * DAY))

    plan = _plan(home, days=90)

    assert plan.remove == [] and plan.scanned == 0


def test_a_tree_with_a_recent_file_is_kept_despite_an_old_top_level(home):
    live = _tree(home / "sessions", "rob", "live", age_days=300,
                 files=("workspace/out.md", "logs/today.log"))
    recent = NOW - 1 * DAY
    os.utime(live / "logs" / "today.log", (recent, recent))
    os.utime(live, (NOW - 300 * DAY, NOW - 300 * DAY))

    plan = _plan(home, days=90)

    assert plan.keep[str(live)] == KEEP_RECENT
    assert plan.remove == []
