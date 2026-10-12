"""W8 (2026-10-10): soul / persona / knowledge / kb resolve the DEPLOYED home.

On a box with a deployed instance and no POLYROB_DATA_DIR in the SSH shell,
``cli._admin_home.admin_data_dir`` adopts the deployed home. These four owner
verbs read ``resolve_data_home`` directly (the shell's ``~/.polyrob/data``), so
``polyrob soul init`` wrote SOUL docs the service never reads.
"""
import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture()
def deployed(tmp_path, monkeypatch):
    home = tmp_path / "deployed"
    home.mkdir()
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr("core.admin_data_home.admin_data_home",
                        lambda echo=None: str(home))
    # the shell's own home is somewhere else entirely
    monkeypatch.setattr("core.runtime_paths.resolve_data_home",
                        lambda: tmp_path / "shell_home")
    return home


def test_soul_init_writes_the_deployed_home(deployed, monkeypatch):
    from cli.commands.soul import soul
    monkeypatch.setattr("click.edit", lambda *a, **k: None)
    res = CliRunner().invoke(soul, ["init", "--no-prompt", "--name", "Rob"])
    assert res.exit_code == 0, res.output
    assert (deployed / "identity" / "identity.md").is_file()


def test_persona_list_reads_the_deployed_home(deployed, monkeypatch):
    seen = {}

    def _listing(uid, home, gate=None):
        seen["home"] = Path(home)
        return {"templates": [], "characters": [], "guidance": []}
    monkeypatch.setattr("cli.persona.build_persona_listing", _listing)
    from cli.commands.persona import persona
    res = CliRunner().invoke(persona, ["list"])
    assert res.exit_code == 0, res.output
    assert seen["home"] == deployed


def test_knowledge_export_reads_the_deployed_home(deployed, monkeypatch):
    async def _no_container(**k):
        raise RuntimeError("no container in this test")
    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _no_container)
    monkeypatch.setattr("modules.memory.backend_factory.maybe_register_memory_backend",
                        lambda **k: None)
    from cli.commands.knowledge import _resolve_provider_and_data_dir
    _provider, data_dir = asyncio.run(_resolve_provider_and_data_dir())
    assert Path(data_dir) == deployed


def test_kb_backend_uses_the_deployed_home(deployed, monkeypatch):
    async def _no_container(**k):
        raise RuntimeError("no container in this test")
    seen = {}
    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _no_container)
    monkeypatch.setattr("modules.memory.backend_factory.maybe_register_memory_backend",
                        lambda **k: seen.update(k))
    from cli.commands.kb import _ensure_memory_backend
    asyncio.run(_ensure_memory_backend())
    assert Path(seen["data_dir"]) == deployed
