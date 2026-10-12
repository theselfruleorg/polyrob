"""W8 (2026-10-10): named store-path helpers + the KB memory.db split.

The goal board, cron store, correspondent registry, surfaces allowlist, session
registry and memory store each have ONE path helper in core.runtime_paths; the
HTTP KB opens the SAME memory.db the agent writes (<data_home>/memory.db), with a
read-both fallback to the legacy <config.data_dir>/memory.db.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture()
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    return home


@pytest.mark.parametrize("helper,name", [
    ("goals_db_path", "goals.db"),
    ("cron_db_path", "cron.db"),
    ("correspondents_db_path", "correspondents.db"),
    ("surfaces_db_path", "surfaces.db"),
    ("session_registry_db_path", "session_registry.db"),
    ("memory_db_path", "memory.db"),
])
def test_named_helpers(_home, tmp_path, helper, name):
    import core.runtime_paths as rp
    fn = getattr(rp, helper)
    assert fn() == os.path.join(str(_home), name)
    assert fn(str(tmp_path / "x")) == os.path.join(str(tmp_path / "x"), name)
    from core.db_manifest import state_db_names
    assert name in state_db_names()


def test_store_db_path_refuses_unregistered_name(_home):
    from core.runtime_paths import store_db_path
    with pytest.raises(KeyError):
        store_db_path("not_a_store.db")


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


def _install_container(monkeypatch, data_dir):
    from core.container import DependencyContainer

    class _C:
        def get_service(self, name):
            return _Cfg(str(data_dir)) if name == "config" else None
    monkeypatch.setattr(DependencyContainer, "get_instance", classmethod(lambda cls, *a, **k: _C()))


def test_kb_memory_dir_is_the_agents_home(_home, monkeypatch):
    """Prod shape: BotConfig.data_dir = <home>/data; the agent's memory.db is <home>/memory.db."""
    _install_container(monkeypatch, _home / "data")
    from api.kb.endpoints import _kb_memory_data_dir
    assert _kb_memory_data_dir() == str(_home)


def test_kb_memory_dir_keeps_a_legacy_only_store(_home, monkeypatch):
    """Read-both: an install whose ONLY memory.db is the legacy one keeps using it."""
    legacy = _home / "data"
    legacy.mkdir()
    (legacy / "memory.db").write_bytes(b"SQLite format 3\x00")
    _install_container(monkeypatch, legacy)
    from api.kb.endpoints import _kb_memory_data_dir
    assert _kb_memory_data_dir() == str(legacy)


def test_kb_memory_dir_prefers_canonical_when_both_exist(_home, monkeypatch):
    legacy = _home / "data"
    legacy.mkdir()
    (legacy / "memory.db").write_bytes(b"SQLite format 3\x00")
    (_home / "memory.db").write_bytes(b"SQLite format 3\x00")
    _install_container(monkeypatch, legacy)
    from api.kb.endpoints import _kb_memory_data_dir
    assert _kb_memory_data_dir() == str(_home)


def test_server_bootstrap_session_root_honours_data_home(_home, monkeypatch):
    """The server bootstrap's path_manager default is resolve_session_data_root(),
    not a bare cwd ./data/task (which ignored POLYROB_DATA_DIR)."""
    monkeypatch.delenv("DATA_ROOT", raising=False)
    from polyrob.app.bootstrap import _server_session_data_root
    assert Path(_server_session_data_root()) == (_home / "sessions").resolve()
