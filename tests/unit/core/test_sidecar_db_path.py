"""R-2 (T1/T2): sidecar_db_path — durable sidecar DBs live on the DATA-HOME axis.

telemetry_events.db and messages.db historically resolved under pm().data_root
(the SESSION artifact tree — <data_home>/sessions on prod-shaped installs) while
the backup manifest expects <data_home>/<name>, so `polyrob update` snapshots
silently missed them. The helper is the one resolution rule: new path preferred;
an existing legacy session-tree file is still returned (read-both, write-new) so
history is never forked across two files.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture()
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("DATA_ROOT", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_fresh_install_resolves_to_data_home(_home):
    from core.runtime_paths import sidecar_db_path
    assert sidecar_db_path("telemetry_events.db") == _home / "telemetry_events.db"


def test_existing_legacy_file_wins_until_relocated(_home):
    """A live install keeps appending to its real (session-tree) file — no fork."""
    from core.runtime_paths import resolve_session_data_root, sidecar_db_path
    legacy = Path(resolve_session_data_root()) / "telemetry_events.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"SQLite format 3\x00")
    assert sidecar_db_path("telemetry_events.db") == legacy


def test_new_file_wins_over_legacy_once_present(_home):
    from core.runtime_paths import resolve_session_data_root, sidecar_db_path
    legacy = Path(resolve_session_data_root()) / "telemetry_events.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"SQLite format 3\x00")
    new = _home / "telemetry_events.db"
    new.write_bytes(b"SQLite format 3\x00")
    assert sidecar_db_path("telemetry_events.db") == new


def test_axis_parity_with_db_manifest(_home):
    """T2 contract: on a fresh home the helper's path IS the manifest's candidate —
    the snapshot can no longer look somewhere the writer doesn't write."""
    from core.db_manifest import candidate_sqlite_dbs
    from core.runtime_paths import resolve_data_home, sidecar_db_path
    cands = {p.resolve() for p in candidate_sqlite_dbs(resolve_data_home())}
    for name in ("telemetry_events.db", "messages.db"):
        assert sidecar_db_path(name).resolve() in cands


def test_event_log_default_uses_sidecar_axis(_home, monkeypatch):
    """The event log's default resolution must route through the helper."""
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_PATH", raising=False)
    import agents.task.telemetry.event_log as ev
    log = ev.get_event_log()
    try:
        assert Path(log.db_path).resolve() == (_home / "telemetry_events.db").resolve()
    finally:
        ev._INSTANCES.pop(str(log.db_path), None)


def test_update_snapshot_captures_legacy_session_tree_files(_home, monkeypatch):
    """T2: a pre-relocation install's real files (session-tree axis) must ride
    the snapshot via extra_dbs — a backup that misses the live DB is data-loss."""
    from core.runtime_paths import resolve_session_data_root
    legacy_dir = Path(resolve_session_data_root())
    legacy_dir.mkdir(parents=True, exist_ok=True)
    for name in ("telemetry_events.db", "messages.db"):
        (legacy_dir / name).write_bytes(b"SQLite format 3\x00")
    from cli.update.context import resolve_update_context
    ctx = resolve_update_context(local=True)
    captured = {p.resolve() for p in ctx.db_paths}
    for name in ("telemetry_events.db", "messages.db"):
        assert (legacy_dir / name).resolve() in captured
