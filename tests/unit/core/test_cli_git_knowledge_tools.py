"""SB-02 / SB-09: git/github must be CLI-registerable, and the REPL must add the
knowledge tool to its loaded tool_ids.

SB-02: the git tool was in _SAFE_LOCAL_FLAGS (ON under POLYROB_LOCAL) but registered
NOWHERE — no register_cli_tools block and dead on the server init path — so an
advertised, safety-engineered capability was 100% dead code. github likewise.

SB-09: the knowledge tool is service-registered under KB_ENABLED (local-ON) but was
never appended to the REPL session's tool_ids, so agent-driven kb_ingest was unreachable.
"""
import asyncio
import inspect
import types

import core.bootstrap as bootstrap
import cli.commands.chat as chat


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


def test_git_and_github_in_cli_registerable_tools():
    assert "git" in bootstrap._CLI_REGISTERABLE_TOOLS
    assert "github" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_git(monkeypatch):
    # Post I-1: git registers via the generic descriptor-driven registrar when enabled.
    monkeypatch.setenv("GIT_TOOLS_ENABLED", "true")
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert c.has_service("git"), "git must resolve to a CLI container service when GIT_TOOLS_ENABLED"


def test_register_cli_tools_registers_github_behind_flag(monkeypatch):
    monkeypatch.setenv("GITHUB_TOOL_ENABLED", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert c.has_service("github"), "github must resolve to a CLI container service when GITHUB_TOOL_ENABLED"


def test_register_cli_tools_omits_github_when_disabled(monkeypatch):
    monkeypatch.setenv("GITHUB_TOOL_ENABLED", "false")
    c = _FakeContainer()
    asyncio.run(bootstrap.register_cli_tools(c))
    assert not c.has_service("github"), "github must NOT register when GITHUB_TOOL_ENABLED is off"


def test_repl_adds_knowledge_tool_when_kb_enabled():
    # SB-09: the REPL start path must append 'knowledge' to repl_tools when KB is on and
    # the service is available.
    # The append lives in the chat REPL start function; assert the wiring is present.
    import cli.commands.chat as chat_mod
    src = inspect.getsource(chat_mod)
    assert 'repl_tools.append("knowledge")' in src
    assert "kb_enabled()" in src
