"""P-final polish: honest /model persistence note + h_mcp manager guard.

- env_default_override_note: /model + `model set-default` must not claim "saved as
  default" without a caveat when an env pin (CHAT_/DEFAULT_PROVIDER) outranks
  cli.json for new sessions.
- _has_zero_arg_list_servers: h_mcp must not hand the MCPTool (list_servers(params))
  to `await manager.list_servers()`; only a zero-arg manager qualifies.
"""
from __future__ import annotations

import cli.config_store as cs
from cli.ui.commands.h_mcp import _has_zero_arg_list_servers, _manager_from_service


# ---- env_default_override_note --------------------------------------------


def test_override_note_when_env_pin_differs(monkeypatch):
    monkeypatch.setattr(cs, "resolve_provider_model", lambda a, b: ("anthropic", "claude-opus-4-8"))
    note = cs.env_default_override_note("openrouter")
    assert note is not None
    assert "anthropic" in note and "precedence" in note


def test_no_note_when_effective_default_matches(monkeypatch):
    monkeypatch.setattr(cs, "resolve_provider_model", lambda a, b: ("openrouter", "z-ai/glm-5.2"))
    assert cs.env_default_override_note("openrouter") is None


def test_override_note_fails_open(monkeypatch):
    def _boom(a, b):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(cs, "resolve_provider_model", _boom)
    assert cs.env_default_override_note("openrouter") is None  # never raises


# ---- h_mcp manager guard --------------------------------------------------


class _ZeroArgManager:
    async def list_servers(self):  # bound method → self excluded, no required args
        return []


class _MCPToolLike:
    # Mimics tools/mcp/mcp_tool.py::MCPTool.list_servers(self, params) — REQUIRES an arg.
    async def list_servers(self, params):
        return []


def test_zero_arg_manager_qualifies():
    assert _has_zero_arg_list_servers(_ZeroArgManager()) is True


def test_param_requiring_tool_is_rejected():
    assert _has_zero_arg_list_servers(_MCPToolLike()) is False


def test_manager_from_service_prefers_server_manager():
    class _Tool:
        server_manager = _ZeroArgManager()
    assert isinstance(_manager_from_service(_Tool()), _ZeroArgManager)


def test_manager_from_service_rejects_bare_mcptool():
    # An MCPTool with no wired server_manager and a param-requiring list_servers must
    # NOT be returned (it would TypeError on `await manager.list_servers()`).
    tool = _MCPToolLike()
    tool.server_manager = None
    assert _manager_from_service(tool) is None
