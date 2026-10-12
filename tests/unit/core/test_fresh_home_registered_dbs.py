"""W8 (2026-10-10): a fresh data home gets only REGISTERED SQLite files.

A representative boot (the CLI container every surface builds + one status
snapshot, which must create nothing) on an empty POLYROB_DATA_DIR. Every
``*.db``/``*.sqlite``/``*.sqlite3`` that appears must have a row in
core.db_manifest (so backup, the shell guard and the path ratchet see it).
"""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_fresh_home_creates_only_registered_dbs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # lazy validation, no call
    with patch("modules.llm.llm_manager.LLMManager._initialize", new=AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        await build_cli_container()

    from core.status_snapshot import build_status_snapshot
    before = set(home.rglob("*"))
    try:
        build_status_snapshot("local", data_dir=str(home))
    except Exception:
        pass
    created_by_status = {p for p in set(home.rglob("*")) - before
                         if p.suffix in (".db", ".sqlite", ".sqlite3")}
    assert not created_by_status, f"a status read created stores: {created_by_status}"

    from core.db_manifest import state_db_names
    dbs = [p for p in home.rglob("*") if p.suffix in (".db", ".sqlite", ".sqlite3")]
    unregistered = sorted(str(p.relative_to(home)) for p in dbs
                          if p.name not in state_db_names())
    assert not unregistered, f"unregistered SQLite files on a fresh home: {unregistered}"
