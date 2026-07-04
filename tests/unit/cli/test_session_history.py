"""A4 — surface compaction checkpoints for recovery.

The compactor writes pre-compaction snapshots to
``sessions/{id}/data/history/compaction_{n}.json``. A4 adds a CLI read path so an
operator can list/inspect them. ``find_compaction_checkpoints`` is the pure finder
under test (no click, no container).
"""
from pathlib import Path

from cli.commands.session import find_compaction_checkpoints


def _make_history(data_root: Path, session_id: str) -> Path:
    hist = data_root / "user42" / "sessions" / session_id / "data" / "history"
    hist.mkdir(parents=True)
    return hist


def test_finds_compaction_checkpoints_sorted(tmp_path):
    hist = _make_history(tmp_path, "sess-abc")
    (hist / "compaction_1.json").write_text("{}")
    (hist / "compaction_2.json").write_text("{}")
    (hist / "compaction_10.json").write_text("{}")
    (hist / "browser_history.json").write_text("{}")  # non-matching

    found = find_compaction_checkpoints(tmp_path, "sess-abc")

    names = [p.name for p in found]
    assert names == ["compaction_1.json", "compaction_2.json", "compaction_10.json"]


def test_partial_session_id_matches(tmp_path):
    hist = _make_history(tmp_path, "sess-abc-12345")
    (hist / "compaction_1.json").write_text("{}")

    found = find_compaction_checkpoints(tmp_path, "sess-abc")
    assert len(found) == 1


def test_no_checkpoints_returns_empty(tmp_path):
    _make_history(tmp_path, "sess-empty")
    assert find_compaction_checkpoints(tmp_path, "sess-empty") == []


def test_unknown_session_returns_empty(tmp_path):
    assert find_compaction_checkpoints(tmp_path, "does-not-exist") == []
