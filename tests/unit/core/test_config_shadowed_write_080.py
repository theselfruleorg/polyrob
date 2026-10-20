"""080 WS-1 items 2 + 3 — a write that a higher layer pins is refused and names
that layer; ``explain`` sees the systemd env file and labels every rung
``env:systemd`` / ``env:file`` / ``env:shell``."""
import pytest

import core.config_service as cs


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    import core.admin_data_home as adh
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: [])
    for k in ("DEFAULT_MODEL", "GOAL_DAILY_QUOTA", "TOOL_AVAILABILITY_HINT"):
        monkeypatch.delenv(k, raising=False)
    cs.reset_file_loaded_env()
    yield
    cs.reset_file_loaded_env()


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    import core.admin_data_home as adh
    f = tmp_path / "polyrob.env"
    f.write_text("DEFAULT_MODEL=deepseek/deepseek-v4.1-flash\n")
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(f))
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: [str(f)])
    return f


def test_systemd_pinned_key_is_refused_and_names_the_file(systemd):
    res = cs.set_value("DEFAULT_MODEL", "x-ai/grok-4.3")
    assert not res.ok and res.outcome == "refused"
    assert "DEFAULT_MODEL" in res.message
    assert str(systemd) in res.message and "wins" in res.message
    assert not (cs.home_env_path()).exists()


def test_same_value_as_the_pin_is_not_refused(systemd):
    res = cs.set_value("DEFAULT_MODEL", "deepseek/deepseek-v4.1-flash")
    assert res.ok, res.message


def test_unreadable_systemd_file_names_it_for_a_service_env_value(tmp_path, monkeypatch):
    """The agent cannot read root-0600 polyrob.env; its env value came from systemd."""
    import core.admin_data_home as adh
    monkeypatch.setattr(adh, "_deployment_evidence", lambda: ["/etc/polyrob/polyrob.env"])

    def _deny(key):
        raise adh.DeployedEnvUnreadable("no")
    monkeypatch.setattr(adh, "deployed_env_value", _deny)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEFAULT_MODEL", "deepseek/deepseek-v4.1-flash")
    res = cs.set_value("DEFAULT_MODEL", "x-ai/grok-4.3", surface="telegram")
    assert not res.ok and res.outcome == "refused"
    assert adh.DEPLOYED_ENV_FILE in res.message


def test_shell_export_is_refused_on_a_local_box(monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    res = cs.set_value("GOAL_DAILY_QUOTA", "7")
    assert not res.ok and res.outcome == "refused"
    assert "environment" in res.message and "wins" in res.message


def test_a_value_loaded_from_the_home_file_does_not_pin(monkeypatch):
    """Sim B: the old value came from the home FILE — not a shell export."""
    home = cs.home_env_path()
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("GOAL_DAILY_QUOTA=9\n")
    monkeypatch.delenv("GOAL_DAILY_QUOTA", raising=False)
    from polyrob.app.bootstrap import load_env
    load_env("development")
    import os
    assert os.environ["GOAL_DAILY_QUOTA"] == "9"
    res = cs.set_value("GOAL_DAILY_QUOTA", "7")
    assert res.ok, res.message
    assert "shell/systemd" not in res.message
    assert cs.process_env_origin("GOAL_DAILY_QUOTA") == "file"


def test_explain_labels_systemd(systemd):
    info = cs.explain("DEFAULT_MODEL")
    origins = [s.origin for s in info.chain]
    assert any(o.startswith("env:systemd") and str(systemd) in o for o in origins)


def test_explain_labels_shell_and_file(monkeypatch):
    home = cs.home_env_path()
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("TOOL_AVAILABILITY_HINT=true\n")
    monkeypatch.setenv("TOOL_AVAILABILITY_HINT", "false")
    origins = [s.origin for s in cs.explain("TOOL_AVAILABILITY_HINT").chain]
    assert "env:shell" in origins
    assert any(o.startswith("env:file") and str(home) in o for o in origins)
