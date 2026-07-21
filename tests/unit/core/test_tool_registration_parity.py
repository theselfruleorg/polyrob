"""P0-1 (2026-07-09) / I-1 (2026-07-10): durable, SYSTEMIC guard for the tool-
registration drift bug CLASS.

A tool that registers a global descriptor but NOT a container service is silently
"not found in container" — it loads nowhere. This exact shape bit shell, git, twitter,
and (2026-07-08, live) the x402 money tools: a FUNDED agent got "Tool 'x402_pay' not
found in container" and reasoned it had no wallet.

Rather than one assertion per incident, this test is DESCRIPTOR-DERIVED: it forces every
optional tool's gate ON, runs the generic CLI registrar, and asserts that EVERY tool
descriptor the CLI can serve (``get_tool_init_order()`` minus the heavy server-only set)
resolves to a container service. A new optional tool is covered automatically — no new
per-tool test needed.
"""
import asyncio
import types

import pytest

# Importing the tools package forces tools/__init__ to run its gated
# register_optional_tool() inserts so the descriptor set is populated.
from tools import TOOL_COMPONENTS  # noqa: F401  (import for side effect)
from tools.descriptors import get_tool_init_order

from core.bootstrap import register_cli_tools, _CLI_REGISTERABLE_TOOLS, cli_unavailable_tools


# Tools that legitimately CANNOT run in the lightweight CLI container — they need the
# heavy server container (MCP clients, or paid-API creds baked into server config).
# Hardcoded independently of core.bootstrap._CLI_INCOMPATIBLE so a change to that
# (security-relevant) exclusion set has to be reconciled here too.
# "email" was moved OUT (2026-07-14): EmailTool's __init__ has no heavy-server
# dependency (see the matching note in core/bootstrap.py).
# "browser"/"browser_manager" moved OUT (2026-07-19): the actual Chromium/playwright
# launch was already lazy; browser_manager just needed a special-case constructor call
# (BaseComponent, not BaseTool shape) — see the matching note in core/bootstrap.py.
# "mcp" was moved OUT (2026-07-20, S3): MCPTool's __init__ is config parsing only —
# server connections live in the deferred initialize() (see the matching note in
# core/bootstrap.py); registration is MCP_ENABLED-gated via _cli_extra_gate.
SERVER_ONLY = {
    "perplexity",
    "collabland", "alchemy",
    "polymarket", "polymarket_data", "hyperliquid", "hyperliquid_data",
}


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


# Every optional-tool gate we force ON so the parity check covers the whole set. Derived
# by hand from the tools' enabled_fn env names (there is no descriptor field exposing the
# gate to derive from); documented here as the single place to extend when a new gate flag
# is added. Posture-gated tools (shell>=1, self_env>=2) honour an explicit *_ENABLED env
# over the frozen compute-posture default, so setting these is sufficient without a
# posture refreeze.
_ALL_FLAGS = [
    "CRON_ENABLED", "GOALS_ENABLED", "TWITTER_ENABLED", "CODE_EXEC_ENABLED",
    "CODING_TOOLS_ENABLED", "ANYSITE_TOOL_ENABLED", "GIT_TOOLS_ENABLED",
    "GITHUB_TOOL_ENABLED", "SHELL_TOOLS_ENABLED", "SELF_ENV_ENABLED", "KB_ENABLED",
    "X402_CLIENT_ENABLED", "X402_INVOICE_ENABLED", "AGENT_WALLET_ENABLED",
    "HF_DEPLOY_ENABLED", "MCP_ENABLED",
]


# twitter/github register on CREDENTIALS / token (not just a flag) — a legitimately
# different pattern. Supply dummy creds so the registration path is exercised; if their
# client construction rejects dummy creds they're excluded from the strict assertion
# (creds-gated, not flag-gated drift).
_CREDS_GATED = {"twitter", "github"}


def _enable_everything(monkeypatch):
    for f in _ALL_FLAGS:
        monkeypatch.setenv(f, "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "z" * 48)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")  # shell(>=1) + self_env(>=2)
    for k in ("TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN",
              "TWITTER_ACCESS_TOKEN_SECRET", "GITHUB_TOKEN"):
        monkeypatch.setenv(k, "dummy")


def test_every_cli_serviceable_descriptor_resolves(monkeypatch):
    """SYSTEMIC parity: every non-server-only tool descriptor must resolve to a CLI
    container service when its gate is ON. Descriptor-derived — no per-tool assertion."""
    _enable_everything(monkeypatch)
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))

    # After the registrar runs, the optional descriptors have materialized, so the
    # descriptor set is complete. Everything the CLI can serve must be a live service.
    expected = [t for t in get_tool_init_order() if t not in SERVER_ONLY]
    missing = [t for t in expected
               if t not in _CREDS_GATED
               and not (c.has_service(t) or c.has_service(f"{t}_tool"))]
    assert missing == [], (
        "these enabled, CLI-serviceable tool descriptors registered NO container service "
        f"(the 'not found in container' drift class): {missing}")


def test_every_enabled_tool_resolves_to_a_container_service(monkeypatch):
    """The hand-maintained capability set must also fully resolve (belt-and-suspenders
    on the derived _CLI_REGISTERABLE_TOOLS vocabulary)."""
    _enable_everything(monkeypatch)
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    flag_gated = _CLI_REGISTERABLE_TOOLS - _CREDS_GATED
    missing = sorted(t for t in flag_gated if not c.has_service(t))
    assert not missing, (
        "these enabled tool_ids registered NO container service (the 'not found in "
        f"container' drift class): {missing}")


def test_registerable_vocabulary_matches_descriptors(monkeypatch):
    """_CLI_REGISTERABLE_TOOLS must stay a DERIVED VIEW of the descriptors: with all
    gates on it equals get_tool_init_order() minus the server-only set, PLUS the one
    documented non-derived exception ("browser" — a runtime alias registered once
    browser_manager initializes, never a descriptor of its own). This is what stops
    the vocabulary drifting unnoticed — add a descriptor tool without wiring it into
    the CLI (or SERVER_ONLY) and this test goes red."""
    _enable_everything(monkeypatch)
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))  # materializes the optional descriptors
    derived = {t for t in get_tool_init_order() if t not in SERVER_ONLY} | {"browser"}
    assert _CLI_REGISTERABLE_TOOLS == derived, (
        "CLI capability vocabulary drifted from the tool descriptors: "
        f"only-in-vocab={sorted(_CLI_REGISTERABLE_TOOLS - derived)} "
        f"only-in-descriptors={sorted(derived - _CLI_REGISTERABLE_TOOLS)}")


def test_money_tools_specifically_register(monkeypatch):
    """The exact 2026-07-08 regression: x402 money tools must reach the container."""
    for f in ("X402_CLIENT_ENABLED", "X402_INVOICE_ENABLED", "AGENT_WALLET_ENABLED"):
        monkeypatch.setenv(f, "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "z" * 48)
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    assert c.has_service("x402_pay"), "x402_pay must resolve to a container service when enabled"
    assert c.has_service("x402_invoice"), "x402_invoice must resolve to a container service when enabled"


def test_browser_tool_specifically_registers(monkeypatch):
    """The 2026-07-19 regression: BrowserManager is a bare BaseComponent
    (`__init__(self, config=None)`), not a BaseTool, so it does NOT fit the generic
    loop's `cls(name, config, container)` calling convention — that mismatch silently
    swallowed by the loop's per-tool try/except made `browser` look identical to a
    genuinely-excluded tool (autonomous goals repeatedly cited "browser tool loaded"
    as a blocker: sessions 53c43cad, 64e9088f, a7ea136f3c9e). Both the manager AND the
    `browser` alias a session actually requests via tool_ids must resolve."""
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    assert c.has_service("browser_manager"), "browser_manager must resolve to a container service"
    assert c.has_service("browser"), "the 'browser' alias tool_ids actually request must resolve"
    assert "browser" not in cli_unavailable_tools(["browser"]), (
        "cli_unavailable_tools() must not falsely report 'browser' as CLI-unavailable")
