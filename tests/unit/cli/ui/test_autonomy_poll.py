"""Autonomy snapshot poll: fail-open, no store creation, correct shape."""
from cli.ui.autonomy_poll import read_autonomy_snapshot


def test_missing_stores_return_none_and_create_nothing(tmp_path):
    snap = read_autonomy_snapshot("local", data_dir=str(tmp_path))
    assert snap is None or snap == {"goals": 0, "cron": 0, "review": True}
    # CRITICAL: polling must never create db files (path-concerns landmine)
    assert not (tmp_path / "cron.db").exists()
    assert not (tmp_path / "goals.db").exists()


def test_never_raises_on_garbage_dir():
    # Contract: fail-open — the call itself must not raise.
    read_autonomy_snapshot("local", data_dir="/nonexistent/nope")


# --- 116 W7.4: the counts are the snapshot's work/loops sections ------------

def _sections(open_goals=None, cron_enabled=None, cron_reason=None):
    from core.status_types import Section
    work = Section(name="work")
    if open_goals is not None:
        work.data["open_goals"] = open_goals
    loops = Section(name="loops")
    if cron_reason:
        loops.data["cron_reason"] = cron_reason
    else:
        loops.data["cron_enabled"] = cron_enabled
    return {"work": work, "loops": loops}


def _touch_stores(tmp_path):
    (tmp_path / "goals.db").write_bytes(b"")
    (tmp_path / "cron.db").write_bytes(b"")


def test_counts_are_the_sections_numbers(tmp_path, monkeypatch):
    from cli.ui import autonomy_poll
    monkeypatch.setattr("core.runtime_paths.goals_db_path", lambda d: str(tmp_path / "goals.db"))
    monkeypatch.setattr("core.runtime_paths.cron_db_path", lambda d: str(tmp_path / "cron.db"))
    _touch_stores(tmp_path)
    secs = _sections(open_goals=[{"id": "g1", "status": "triage"}, {"id": "g2", "status": "ready"}],
                     cron_enabled=1)
    assert autonomy_poll.counts_from_sections(secs, str(tmp_path)) == (2, 1, {})


def test_an_unreadable_store_is_none_never_zero(tmp_path, monkeypatch):
    from cli.ui import autonomy_poll
    from core.status_types import STATE_UNAVAILABLE, Section
    monkeypatch.setattr("core.runtime_paths.goals_db_path", lambda d: str(tmp_path / "goals.db"))
    monkeypatch.setattr("core.runtime_paths.cron_db_path", lambda d: str(tmp_path / "cron.db"))
    _touch_stores(tmp_path)
    secs = _sections(cron_reason="DatabaseError: file is not a database")
    secs["work"] = Section(name="work", state=STATE_UNAVAILABLE, reason="DatabaseError: x")
    goals, cron, unread = autonomy_poll.counts_from_sections(secs, str(tmp_path))
    assert goals is None and cron is None
    assert "DatabaseError" in unread["goals"] and "DatabaseError" in unread["cron"]


def test_a_missing_store_is_zero(tmp_path):
    from cli.ui import autonomy_poll
    assert autonomy_poll.counts_from_sections(_sections(), str(tmp_path)) == (0, 0, {})


def test_a_corrupt_goal_board_reads_unreadable_end_to_end(tmp_path, monkeypatch):
    from cli.ui import autonomy_poll
    from core.runtime_paths import goals_db_path
    p = goals_db_path(str(tmp_path))
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(b"this is not a sqlite file" * 64)
    snap = autonomy_poll.read_autonomy_snapshot("local", data_dir=str(tmp_path))
    assert snap is not None and snap["goals"] is None and "goals" in snap["unread"]
