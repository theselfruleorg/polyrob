"""031: the ONE durable pause record + the allows() predicate."""
import json
import os
import time

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "TREASURY_ENTRY_PAUSE", "STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    # One base only: the conftest autouse fixture points POLYROB_DATA_DIR at a
    # throwaway dir; pin it AND the resolved home to tmp_path so state_bases()
    # dedupes to exactly one directory.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def test_default_is_not_paused(home):
    from core import autonomy_control as ac
    st = ac.read_state(str(home))
    assert st.paused is False and st.scopes == () and st.source == "none"
    assert ac.allows("dispatch", str(home)).allowed is True


def test_pause_all_denies_every_kind_and_is_durable(home):
    from core import autonomy_control as ac
    res = ac.pause(str(home), scopes=("all",), set_by="owner", via="test", reason="stop")
    assert res.effective is True
    assert (home / ac.PAUSE_FILENAME).exists()
    for kind in ac.KIND_SCOPES:
        assert ac.allows(kind, str(home)).allowed is False, kind
    rec = json.loads((home / ac.PAUSE_FILENAME).read_text())
    assert rec["scopes"] == ["all"] and rec["set_by"] == "owner" and rec["via"] == "test"


def test_scoped_pause_denies_only_its_kinds(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("streams",))
    assert ac.allows("seed_stream", str(home)).allowed is False
    assert ac.allows("dispatch", str(home)).allowed is True
    assert ac.allows("trade_entry", str(home)).allowed is True


def test_unknown_kind_is_denied_while_any_pause_is_on(home):
    from core import autonomy_control as ac
    assert ac.allows("something_new", str(home)).allowed is True
    ac.pause(str(home), scopes=("pings",))
    assert ac.allows("something_new", str(home)).allowed is False


def test_expired_pause_auto_resumes_and_removes_file(home, monkeypatch):
    from core import autonomy_control as ac
    ac.pause(str(home), duration_minutes=1)
    assert ac.allows("dispatch", str(home)).allowed is False
    monkeypatch.setattr(ac, "_now", lambda: time.time() + 120)
    assert ac.allows("dispatch", str(home)).allowed is True
    assert not (home / ac.PAUSE_FILENAME).exists()


def test_unreadable_record_fails_closed(home):
    from core import autonomy_control as ac
    (home / ac.PAUSE_FILENAME).write_text("{not json")
    st = ac.read_state(str(home))
    assert st.paused is True and st.scopes == ("all",) and st.source == "unreadable"


def test_legacy_files_and_env_are_read_as_facets(home, monkeypatch):
    from core import autonomy_control as ac
    (home / "TREASURY_ENTRY_PAUSE").write_text("x")
    st = ac.read_state(str(home))
    assert st.paused and st.scopes == ("trading",) and st.source == "legacy"
    (home / "STREAM_SEEDING_PAUSE").write_text("x")
    assert set(ac.read_state(str(home)).scopes) == {"trading", "streams"}
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    assert ac.read_state(str(home)).scopes == ("all",)
    assert ac.read_state(str(home)).source == "env"


def test_resume_clears_record_and_legacy_files(home):
    from core import autonomy_control as ac
    (home / "AUTONOMY_HALT").write_text("x")
    ac.pause(str(home), scopes=("cron",))
    res = ac.resume(str(home))
    assert res.state.paused is False
    assert res.effective is True
    assert not (home / "AUTONOMY_HALT").exists()
    assert not (home / ac.PAUSE_FILENAME).exists()


def test_resume_one_scope_keeps_the_others(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("cron", "streams"))
    res = ac.resume(str(home), scopes=("cron",))
    assert res.state.scopes == ("streams",)
    assert res.effective is True


def test_scoped_pause_merges_into_an_existing_record(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("cron",))
    ac.pause(str(home), scopes=("streams",))
    assert set(ac.read_state(str(home)).scopes) == {"cron", "streams"}
    ac.pause(str(home), scopes=("all",))
    assert ac.read_state(str(home)).scopes == ("all",)


def test_env_halt_cannot_be_resumed_and_result_says_so(home, monkeypatch):
    from core import autonomy_control as ac
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    res = ac.resume(str(home))
    assert res.state.paused is True and res.state.source == "env"
    assert res.effective is False


def test_transition_hook_fires_with_old_and_new(home):
    from core import autonomy_control as ac
    seen = []
    ac.register_transition_hook(lambda old, new: seen.append((old.paused, new.paused)))
    try:
        ac.pause(str(home))
        ac.resume(str(home))
    finally:
        ac._TRANSITION_HOOKS.clear()
    assert seen == [(False, True), (True, False)]


def test_write_is_atomic_no_tmp_left_behind(home):
    from core import autonomy_control as ac
    ac.pause(str(home))
    assert not any(p.name.endswith(".tmp") for p in home.iterdir())


def test_audit_row_written(home):
    from core import autonomy_control as ac
    from core.sqlite_util import execute_retry
    ac.pause(str(home), reason="why")
    rows = execute_retry(str(home / "autonomy_state.db"),
                         "SELECT action, reason FROM control_events", (), fetch="all")
    assert [(r["action"], r["reason"]) for r in rows] == [("pause", "why")]


def test_read_never_creates_a_record(home):
    from core import autonomy_control as ac
    ac.read_state(str(home))
    ac.allows("dispatch", str(home))
    assert not (home / ac.PAUSE_FILENAME).exists()
    assert not (home / "autonomy_state.db").exists()


def test_merge_never_shortens_an_existing_pause(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("streams",))
    st = ac.pause(str(home), scopes=("cron",), duration_minutes=90).state
    assert set(st.scopes) == {"streams", "cron"} and st.until is None
    ac.resume(str(home))
    ac.pause(str(home), scopes=("streams",), duration_minutes=30)
    st = ac.pause(str(home), scopes=("cron",), duration_minutes=90).state
    assert st.until is not None and st.until - time.time() > 80 * 60
    # a narrower pause on top of "all" keeps "all"
    ac.pause(str(home), scopes=("all",))
    assert ac.pause(str(home), scopes=("cron",)).state.scopes == ("all",)


def test_scoped_resume_never_narrows_a_full_pause(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",))
    res = ac.resume(str(home), scopes=("trading",))
    assert res.effective is False and res.state.scopes == ("all",) and "full pause" in res.note
    assert ac.allows("dispatch", str(home)).allowed is False
    assert ac.resume(str(home)).state.paused is False


def test_expired_record_never_hides_a_legacy_file_in_the_same_base(home, monkeypatch):
    from core import autonomy_control as ac
    ac.pause(str(home), duration_minutes=1)
    (home / "AUTONOMY_HALT").write_text("")
    monkeypatch.setattr(ac, "_now", lambda: time.time() + 120)
    assert ac.allows("dispatch", str(home)).allowed is False
    assert ac.read_state(str(home)).file_scopes == ("all",)


def test_scoped_pause_under_an_env_halt_is_effective(home, monkeypatch):
    from core import autonomy_control as ac
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    res = ac.pause(str(home), scopes=("cron",))
    assert res.effective is True and res.state.scopes == ("all",)
    assert res.state.record_scopes == ("cron",) and res.state.env_scopes == ("all",)


def test_facets_are_tracked_and_a_scoped_resume_keeps_only_record_scopes(home, monkeypatch):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("cron",))
    monkeypatch.setenv("TREASURY_ENTRY_PAUSE", "true")
    st = ac.read_state(str(home))
    assert set(st.scopes) == {"cron", "trading"} and st.record_scopes == ("cron",)
    assert st.env_scopes == ("trading",) and st.source == "record"
    res = ac.resume(str(home), scopes=("trading",))
    assert res.effective is False and "ENVIRONMENT" in res.note and "TREASURY_ENTRY_PAUSE" in res.note
    assert ac.read_state(str(home)).record_scopes == ("cron",)  # the env facet was never baked in
    res = ac.resume(str(home), scopes=("cron",))
    assert res.effective is True and ac.read_state(str(home)).scopes == ("trading",)


def test_multi_base_write_and_resume(home, monkeypatch):
    other = home / "other"
    from core import autonomy_control as ac
    res = ac.pause(str(other), scopes=("streams",))
    assert sorted(res.written) == sorted([str(other / ac.PAUSE_FILENAME), str(home / ac.PAUSE_FILENAME)])
    assert ac.read_state(str(home)).scopes == ("streams",)
    assert ac.read_state(None).scopes == ("streams",)   # a reader with no data_dir sees it too
    res = ac.resume(str(other))
    assert res.effective is True and not (home / ac.PAUSE_FILENAME).exists()
    assert not (other / ac.PAUSE_FILENAME).exists()


def test_paused_false_record_never_masks_another_base(home):
    from core import autonomy_control as ac
    other = home / "other"
    other.mkdir()
    ac.pause(str(home))
    (other / ac.PAUSE_FILENAME).write_text(json.dumps({"paused": False, "scopes": ["all"], "since": time.time() + 99}))
    assert ac.read_state(str(other)).paused is True


def test_repeated_stop_keeps_the_original_since(home, monkeypatch):
    from core import autonomy_control as ac
    first = ac.pause(str(home)).state.since
    monkeypatch.setattr(ac, "_now", lambda: time.time() + 600)
    assert ac.pause(str(home)).state.since == first


def test_concurrent_pauses_never_corrupt_the_record(home):
    import threading
    from core import autonomy_control as ac
    errs = []

    def _p(scope):
        try:
            ac.pause(str(home), scopes=(scope,))
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=_p, args=(sc,)) for sc in ("cron", "streams", "social", "pings")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    st = ac.read_state(str(home))
    assert not errs and st.source == "record" and set(st.scopes) == {"cron", "streams", "social", "pings"}
    assert not any(p.name.endswith(".tmp") for p in home.iterdir())


def test_a_touched_legacy_pause_reports_when_it_began(home):
    """A `touch <data>/AUTONOMY_HALT` has no record, so `since` came out None and
    every seat rendered "since ?" — the owner could not tell a pause set minutes
    ago from one set last week, and the violation detector had no window."""
    from core import autonomy_control as ac
    (home / "AUTONOMY_HALT").write_text("")
    old = time.time() - 3600
    os.utime(home / "AUTONOMY_HALT", (old, old))
    st = ac.read_state(str(home))
    assert st.paused and st.source == "legacy"
    assert st.since is not None and abs(st.since - old) < 2
    from core.status_render import pause_headline_from
    assert "since ?" not in pause_headline_from(st.to_dict())
