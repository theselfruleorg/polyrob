"""R-2 (T3): one-shot relocation of legacy session-tree sidecar DBs to the data home.

Triggered from the event log's DEFAULT resolution (first telemetry touch in a
process) so the singleton binds to the new path from the start — no rebind, no
history fork in the mover process. Fail-open and clobber-proof: an existing
file at the new path always aborts the move (os.link is the atomic no-clobber
primitive), and a zero-byte legacy file (a racing process's connect artifact)
is never moved.
"""
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("DATA_ROOT", raising=False)
    (tmp_path / "home").mkdir()
    import core.sidecar_relocate as sr
    monkeypatch.setattr(sr, "_DONE", False)
    return tmp_path / "home"


def _make_sqlite(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def test_relocates_legacy_file_to_data_home(_home):
    from core.runtime_paths import resolve_session_data_root
    from core.sidecar_relocate import relocate_legacy_sidecars
    legacy = Path(resolve_session_data_root()) / "telemetry_events.db"
    _make_sqlite(legacy, "keepme")
    moved = relocate_legacy_sidecars()
    assert "telemetry_events.db" in moved
    assert not legacy.exists()
    new = _home / "telemetry_events.db"
    conn = sqlite3.connect(new)
    assert conn.execute("SELECT v FROM t").fetchone() == ("keepme",)
    conn.close()


def test_never_clobbers_an_existing_new_file(_home):
    from core.runtime_paths import resolve_session_data_root
    from core.sidecar_relocate import relocate_legacy_sidecars
    legacy = Path(resolve_session_data_root()) / "telemetry_events.db"
    _make_sqlite(legacy, "old")
    new = _home / "telemetry_events.db"
    _make_sqlite(new, "new")
    moved = relocate_legacy_sidecars()
    assert "telemetry_events.db" not in moved
    conn = sqlite3.connect(new)
    assert conn.execute("SELECT v FROM t").fetchone() == ("new",)
    conn.close()
    assert legacy.exists()  # left in place — helper still reads... new wins; file kept for the operator


def test_zero_byte_legacy_artifact_is_ignored(_home):
    from core.runtime_paths import resolve_session_data_root
    from core.sidecar_relocate import relocate_legacy_sidecars
    legacy = Path(resolve_session_data_root()) / "messages.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.touch()
    assert "messages.db" not in relocate_legacy_sidecars()


def test_event_log_default_resolution_triggers_relocation(_home, monkeypatch):
    from core.runtime_paths import resolve_session_data_root
    legacy = Path(resolve_session_data_root()) / "telemetry_events.db"
    _make_sqlite(legacy, "pre-move")
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_PATH", raising=False)
    import agents.task.telemetry.event_log as ev
    log = ev.get_event_log()
    try:
        assert Path(log.db_path).resolve() == (_home / "telemetry_events.db").resolve()
        assert not legacy.exists()
    finally:
        ev._INSTANCES.pop(str(log.db_path), None)
