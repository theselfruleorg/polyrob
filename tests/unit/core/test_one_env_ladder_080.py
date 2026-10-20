"""080 WS-1 item 7 — one env ladder for every entry point.

The server ladder (``config/.env.*`` loaded with override=True, which beat the
systemd ``EnvironmentFile``) is deleted: ``build_bot``, ``api.app`` and
``api.server_boot`` read the same ladder as the CLI — process env > the
instance file — so a systemd value always wins."""
import os


def test_api_entry_point_ladder_keeps_the_systemd_value(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".env.production").write_text("ONE_LADDER_PROBE=from_config_file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("ONE_LADDER_PROBE", "from_systemd")
    from polyrob.app.bootstrap import load_env
    load_env("production")  # what api.app / api.server_boot / build_bot call
    assert os.environ["ONE_LADDER_PROBE"] == "from_systemd"


def test_api_entry_point_reads_the_home_file(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / ".env").write_text("ONE_LADDER_PROBE2=from_home\n")
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("ONE_LADDER_PROBE2", raising=False)
    from polyrob.app.bootstrap import load_env
    load_env()
    assert os.environ.get("ONE_LADDER_PROBE2") == "from_home"
    monkeypatch.delenv("ONE_LADDER_PROBE2", raising=False)
