import json
from pathlib import Path
from tools.mcp.config import load_local_mcp_servers


def test_project_overrides_global(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"; (proj / ".polyrob").mkdir(parents=True)
    (home / ".polyrob" / "mcp.json").write_text(json.dumps({"servers": {
        "global_only": {"command": "g"}, "shared": {"command": "from_home"}}}))
    (proj / ".polyrob" / "mcp.json").write_text(json.dumps({"servers": {
        "proj_only": {"command": "p"}, "shared": {"command": "from_proj"}}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    servers = load_local_mcp_servers()
    assert set(servers) == {"global_only", "shared", "proj_only"}
    assert servers["shared"]["command"] == "from_proj"  # project wins on clash


def test_mcpServers_key_supported(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".polyrob").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)  # no ./.polyrob
    (home / ".polyrob" / "mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "c"}}}))
    assert load_local_mcp_servers() == {"x": {"command": "c"}}


def test_missing_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    monkeypatch.chdir(tmp_path)
    assert load_local_mcp_servers() == {}
