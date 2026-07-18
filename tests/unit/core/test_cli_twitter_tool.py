"""Regression tests for the twitter tool being loadable in the headless/CLI container.

Bug (2026-07-01): autonomous goal sessions loaded only filesystem+task because
`load_tools_from_container` resolves tool_ids to container *services*, but
`register_cli_tools` never registered a `twitter` service and `_CLI_REGISTERABLE_TOOLS`
excluded it — so an agent with `--tools twitter` had no twitter_* actions and could
never post. Fix: register `twitter` when X creds are present + demote the unused
`database_manager` from required→optional so the lightweight container can init it.
"""
import asyncio
import pathlib

import core.bootstrap as bootstrap

_TWITTER_TOOL = pathlib.Path(__file__).resolve().parents[3] / "tools" / "twitter_tool.py"


class _Container:
    """Minimal container backed by a REAL BotConfig — TwitterTool.__init__ calls
    config.get_twitter_config(), so a bare SimpleNamespace would fail construction."""
    def __init__(self, config):
        self._svc = {}
        self.config = config

    def has_service(self, name):
        return name in self._svc

    def register_service(self, name, obj):
        self._svc[name] = obj

    def register_required_service(self, name, obj):
        self._svc[name] = obj

    def get_service(self, name):
        return self._svc.get(name)


def _container(monkeypatch, tmp_path):
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    from core.config import BotConfig
    return _Container(BotConfig())


def test_twitter_in_cli_registerable_tools():
    assert "twitter" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_twitter_when_creds_present(monkeypatch, tmp_path):
    # Gated on credentials — registered as the `twitter` service when X creds are set.
    monkeypatch.setenv("TWITTER_API_KEY", "dummy")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "dummy")
    c = _container(monkeypatch, tmp_path)
    asyncio.run(bootstrap.register_cli_tools(c))
    assert c.has_service("twitter"), "twitter must resolve to a CLI container service when creds present"


def test_register_cli_tools_omits_twitter_without_creds(monkeypatch, tmp_path):
    monkeypatch.delenv("TWITTER_API_KEY", raising=False)
    monkeypatch.delenv("TWITTER_ACCESS_TOKEN", raising=False)
    c = _container(monkeypatch, tmp_path)
    asyncio.run(bootstrap.register_cli_tools(c))
    assert not c.has_service("twitter"), "twitter must NOT register without X credentials"


def test_twitter_tool_does_not_hard_require_database_manager():
    # Read the source directly — importing tools.twitter_tool triggers the heavy
    # tools package __init__ which isn't importable in the lightweight test env.
    text = _TWITTER_TOOL.read_text()
    assert "def required_services" in text
    required_src = text.split("def required_services", 1)[1].split("def optional_services", 1)[0]
    optional_src = text.split("def optional_services", 1)[1].split("def __init__", 1)[0]
    # rate_limit_manager stays required; database_manager must NOT be required
    # (the lightweight headless container has no database_manager).
    assert "rate_limit_manager" in required_src
    assert "database_manager" not in required_src
    # ...but it remains available as an optional service when a server provides it.
    assert "database_manager" in optional_src
