"""Autonomy tools (cronjob / goal) must be CLI-container-registered behind their flags.

Post I-1 (2026-07-10) these register via the generic descriptor-driven registrar rather
than a hand-written per-tool block, so this asserts BEHAVIOR (the service resolves) rather
than the source shape.
"""
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


def test_cronjob_and_goal_in_cli_registerable_tools():
    assert "cronjob" in bootstrap._CLI_REGISTERABLE_TOOLS
    assert "goal" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_cronjob_and_goal_when_enabled(monkeypatch):
    monkeypatch.setenv("CRON_ENABLED", "true")
    monkeypatch.setenv("GOALS_ENABLED", "true")
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert c.has_service("cronjob"), "cronjob must resolve to a CLI container service when CRON_ENABLED"
    assert c.has_service("goal"), "goal must resolve to a CLI container service when GOALS_ENABLED"


def test_register_cli_tools_omits_cronjob_and_goal_when_disabled(monkeypatch):
    monkeypatch.setenv("CRON_ENABLED", "false")
    monkeypatch.setenv("GOALS_ENABLED", "false")
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert not c.has_service("cronjob")
    assert not c.has_service("goal")
