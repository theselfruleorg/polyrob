"""The captured-session X rail must be loadable by CLI/headless agents."""
import asyncio
import types

import core.bootstrap as bootstrap


class _FakeContainer:
    def __init__(self):
        self._svc = {}
        self.config = types.SimpleNamespace()

    def has_service(self, name):
        return name in self._svc

    def register_service(self, name, obj):
        self._svc[name] = obj

    def register_required_service(self, name, obj):
        self._svc[name] = obj

    def get_service(self, name):
        return self._svc.get(name)


def test_x_browser_is_cli_registerable():
    assert "x_browser" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_x_browser_when_enabled(monkeypatch):
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    container = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(container))
    assert container.has_service("x_browser")


def test_register_cli_tools_omits_x_browser_when_disabled(monkeypatch):
    monkeypatch.setenv("X_BROWSER_ENABLED", "false")
    container = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(container))
    assert not container.has_service("x_browser")
