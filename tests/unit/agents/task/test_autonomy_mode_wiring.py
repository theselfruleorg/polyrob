"""Each capability flag defaults ON under effective autonomous mode; explicit env wins;
supervised mode is byte-identical (default OFF)."""
import pytest

from agents.task import constants


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()


CASES = [
    # (import path, callable, env var)
    ("agents.task.constants", "message_autonomous_allowlisted", "MESSAGE_AUTONOMOUS_ALLOWLISTED"),
    ("tools.twitter_tool", "twitter_write_enabled", "TWITTER_ENABLED"),
    ("agents.task.surface_config", "SurfaceConfig.email_surface_enabled", "EMAIL_SURFACE_ENABLED"),
    ("agents.task.surface_config", "SurfaceConfig.group_chat_enabled", "GROUP_CHAT_ENABLED"),
    ("agents.task.surface_config", "SurfaceConfig.correspondent_access_enabled", "CORRESPONDENT_ACCESS_ENABLED"),
    ("agents.task.surface_config", "SurfaceConfig.correspondent_reply_enabled", "CORRESPONDENT_REPLY_ENABLED"),
    ("tools.x402", "x402_invoicing_enabled", "X402_INVOICE_ENABLED"),
]


def _resolve(path, name):
    import importlib
    mod = importlib.import_module(path)
    obj = mod
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize("path,name,env", CASES)
def test_defaults_off_supervised(monkeypatch, path, name, env):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv(env, raising=False)
    assert _resolve(path, name)() is False


@pytest.mark.parametrize("path,name,env", CASES)
def test_defaults_on_autonomous(monkeypatch, path, name, env):
    _enable_full(monkeypatch)
    monkeypatch.delenv(env, raising=False)
    assert _resolve(path, name)() is True


@pytest.mark.parametrize("path,name,env", CASES)
def test_explicit_env_off_wins_over_mode(monkeypatch, path, name, env):
    _enable_full(monkeypatch)
    monkeypatch.setenv(env, "false")
    assert _resolve(path, name)() is False


def test_require_approval_inverts_under_autonomous(monkeypatch):
    from agents.task.surface_config import SurfaceConfig
    monkeypatch.delenv("CORRESPONDENT_REQUIRE_APPROVAL", raising=False)
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    assert SurfaceConfig.correspondent_require_approval() is True   # supervised: ON
    _enable_full(monkeypatch)
    assert SurfaceConfig.correspondent_require_approval() is False  # autonomous: auto-ratify
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    assert SurfaceConfig.correspondent_require_approval() is True   # explicit wins


def test_mcp_enabled_default_on_under_autonomous(monkeypatch):
    """modules/eip8004/registration.py's INDEPENDENT MCP_ENABLED OR (agent-card
    endpoint list) must agree with core/config.py's consumer seam — see
    test_mcp_config_built_under_autonomous_mode below for the config.mcp seam
    itself. Hermetic — no real servers."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from agents.task.constants import _mode_capability_default
    assert _mode_capability_default("MCP_ENABLED") is True

    from modules.eip8004.registration import build_registration_file
    reg = build_registration_file(base_url="http://localhost:9000")
    names = {ep.name for ep in reg.endpoints}
    assert "MCP" in names


def test_mcp_enabled_stays_off_supervised(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from modules.eip8004.registration import build_registration_file
    reg = build_registration_file(base_url="http://localhost:9000")
    names = {ep.name for ep in reg.endpoints}
    assert "MCP" not in names


def test_mcp_config_built_under_autonomous_mode(monkeypatch):
    """Integration test at the actual consumer seam (core/config.py::
    _build_mcp_config_from_env): under autonomous mode with MCP_ENABLED unset,
    config.mcp is built and enabled the same way an explicit MCP_ENABLED=true
    would build it. Hermetic — no real servers (the checked-in
    config/mcp_config.json ships an empty servers map)."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from core.config import BotConfig
    config = BotConfig()
    assert config.mcp is not None
    assert config.mcp.get("enabled") is True


def test_mcp_config_stays_none_supervised(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from core.config import BotConfig
    config = BotConfig()
    assert config.mcp is None
