"""098 §3.6: PhaseJournal."""
import json

import pytest

from core.state.journal import JournalBusy, PhaseJournal
from core.state.machine import IllegalTransition


def test_begin_phase_done(tmp_path):
    j = PhaseJournal(str(tmp_path / "op.json"))
    assert j.pending() is None
    j.begin("update", owner="pid-1", rollback_info={"old_sha": "abc"})
    j.phase("fetched")
    j.phase("installed", new_sha="def")
    p = j.pending()
    assert p["op"] == "update" and p["phase"] == "installed"
    assert p["rollback"] == {"old_sha": "abc"} and p["info"]["new_sha"] == "def"
    assert p["phases"] == ["begin", "fetched", "installed"]
    j.done()
    assert j.pending() is None and j.read()["state"] == "done"


def test_crash_leaves_pending_and_begin_refuses(tmp_path):
    path = tmp_path / "op.json"
    PhaseJournal(str(path)).begin("release", owner="x")
    PhaseJournal(str(path)).phase("tagged")
    # a new process
    j = PhaseJournal(str(path))
    assert j.pending()["phase"] == "tagged"
    with pytest.raises(JournalBusy):
        j.begin("release")
    j.begin("release", force=True)
    assert j.pending()["phase"] == "begin"


def test_failed_records_error(tmp_path):
    j = PhaseJournal(str(tmp_path / "op.json"))
    j.begin("draw")
    j.failed(RuntimeError("boom"))
    assert j.pending() is None and j.read()["error"] == "boom"
    j.begin("draw")  # a finished op does not block a new one


def test_phase_after_done_is_illegal(tmp_path):
    j = PhaseJournal(str(tmp_path / "op.json"))
    j.begin("x")
    j.done()
    with pytest.raises(IllegalTransition):
        j.done()


def test_torn_file_reads_as_no_op(tmp_path):
    path = tmp_path / "op.json"
    path.write_text("{not json")
    assert PhaseJournal(str(path)).pending() is None


def test_clear(tmp_path):
    j = PhaseJournal(str(tmp_path / "op.json"))
    j.begin("x")
    with pytest.raises(JournalBusy):
        j.clear()
    j.done()
    j.clear()
    assert j.read() is None


def test_done_records_the_last_phase_in_one_write(tmp_path, monkeypatch):
    """098 review: phase + done were two writes; a kill between them left an
    applied update `running` and the next boot rolled it back."""
    from core.state import journal as jmod
    j = jmod.PhaseJournal(str(tmp_path / "j.json"))
    j.begin("update")
    writes = []
    real = jmod.atomic_write_json
    monkeypatch.setattr(jmod, "atomic_write_json", lambda p, o, **k: (writes.append(dict(o)), real(p, o, **k)))
    j.done(phase="applied")
    assert len(writes) == 1
    assert (writes[0]["state"], writes[0]["phase"]) == ("done", "applied")
    assert j.pending() is None
