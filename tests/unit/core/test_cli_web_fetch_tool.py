"""Regression: web_fetch must be registered as a service in the headless/CLI container.

Bug (2026-07-01, capability test): `web_fetch` is in `_CLI_REGISTERABLE_TOOLS` and has a real
tool class, but `register_cli_tools` never registered a `web_fetch` service — so a goal/headless
session with `--tools web_fetch` silently had no web_fetch action (agent reported "web_fetch is
NOT available"). Same class as the twitter-loading bug. Post I-1 (2026-07-10) web_fetch registers
via the generic descriptor-driven registrar; this asserts the BEHAVIOR (the service resolves).
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


def test_web_fetch_in_cli_registerable_tools():
    assert "web_fetch" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_web_fetch():
    # web_fetch is unconditional (no flag / creds), so it must always resolve.
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert c.has_service("web_fetch"), "web_fetch must resolve to a CLI container service"
