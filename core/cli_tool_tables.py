"""The CLI container's tool tables — pure derivations of ``core.tool_capabilities``.

Split out of the composition root (``polyrob.app.bootstrap``) so a lower tier
(``agents.task.tool_defaults``) can ask "can the CLI register this tool?" without
importing the wiring layer. The bootstrap re-binds these names for its registrar.
"""
from core.tool_capabilities import (
    TOOL_CAPABILITIES as _TOOL_ROWS,
    descriptor_id as _descriptor_id,
    ids_where as _tool_ids_where,
    row_field as _tool_row_field,
)

# Tools the lightweight CLI container CANNOT provide — they need the heavy server
# container (MCP clients, or paid-API creds baked into server config). This is the
# SSOT of CLI exclusions: the generic registrar skips exactly these; everything else
# in the tool descriptors is CLI-registerable.
# NOTE (2026-07-14): "email" was removed from this set — EmailTool's __init__ only
# touches config attrs (no network I/O; SMTP is tested lazily in the async
# _initialize(), which the CLI already defers until a session first loads the tool),
# so it needs no heavy server-only dependency. Leaving it excluded meant the `email`
# TOOL (agent-callable send action) never registered a container service on EITHER
# CLI-built process (`polyrob telegram` or `polyrob email`) even though creds were
# configured and the email SURFACE (harness, built separately in
# `cli/commands/email.py`) worked fine — every goal requesting `tools=["email"]` hit
# "✗ Tool 'email' not found in container" and reported a false "no credential"
# blocker (observed live 2026-07-14).
# NOTE (2026-07-19): "browser"/"browser_manager" removed from this set for the SAME
# false-blocker reason — the actual Chromium/playwright launch was ALREADY lazy
# (`Browser._initialize()`'s own docstring: "browser instance will start on first
# use"), so excluding it bought nothing except a silent, misleading "browser tool not
# available" for every session that requested it (autonomous goals citing this as a
# blocker: sessions 53c43cad, 64e9088f, and the still-open a7ea136f3c9e). The real
# obstacle was structural, not weight: BrowserManager is a bare BaseComponent
# (`__init__(self, config=None)`), not a BaseTool, so it doesn't fit the generic
# loop's `cls(name, config, container)` calling convention — see the dedicated
# special-case block in `register_cli_tools` below (mirrors the identical special
# case already in `polyrob/app/initialization.py::initialize_tools` on the server path,
# which is how this was confirmed safe rather than assumed).
# NOTE (2026-07-20, S3 dynamic tool rig — same precedent again): "mcp" removed.
# MCPTool.__init__ is config parsing only (attrs + `${VAR}` placeholder resolution,
# fail-open to defaults; server CONNECTIONS live in the deferred async initialize(),
# which the CLI already defers until a session first loads the tool). Registration
# is gated by the mcp tool's own gate (tools/mcp/config.py::mcp_gate_on: explicit
# MCP_ENABLED, else the operator's local server file) — absent secrets make the affected
# server connections fail loudly at load time (an owner ask), never a silent
# "not found in container".
#
# 067 P1: the three tables below are VIEWS of the ``cli``/``cli_registrar`` fields of
# the per-tool rows in core/tool_capabilities.py — change a tool's CLI mode there.
_CLI_INCOMPATIBLE = set(_tool_ids_where("cli", "incompatible"))


def _derive_cli_optional_registrars() -> tuple:
    """``((module, function, services), ...)`` from the rows' ``cli_registrar``.

    Optional (flag/posture/creds-gated) tools the CLI can register. Each registrar is
    the ``register_optional_tool()`` wrapper that MATERIALIZES the descriptor under
    the CURRENT env (self-gating — a no-op when its flag/posture is off), plus the
    container service name(s) it produces. This ONE table drives BOTH descriptor
    materialization (so the generic loop below sees the enabled optionals even when
    tools/__init__ was first imported with the flags off) AND the derived
    _CLI_REGISTERABLE_TOOLS capability set. Rows that share a registrar group into
    one entry in row order (``shell`` registers two services: shell + process). The
    money/high-impact DeFi verbs (defi_trade/launchpad/dapp_browser) are ``cli="none"``;
    only the read-only book (``defi_data``) belongs on the CLI default rig.
    Module paths are strings: core imports no tool module here.
    """
    grouped: dict = {}
    for tool_id in _TOOL_ROWS:
        ref = _tool_row_field(tool_id, "cli_registrar")
        if ref:
            grouped.setdefault(ref, []).append(tool_id)
    return tuple((ref.partition(":")[0], ref.partition(":")[2], tuple(services))
                 for ref, services in grouped.items())


_CLI_OPTIONAL_REGISTRARS = _derive_cli_optional_registrars()

# Static (always-present) descriptors the CLI serves — the lightweight, dependency-free
# tools that are in tools/descriptors.py unconditionally, named by DESCRIPTOR id (the
# ``browser`` row is the ``browser_manager`` descriptor). The parity test asserts this
# stays == get_tool_init_order() - _CLI_INCOMPATIBLE once all descriptors materialize.
_CLI_STATIC_TOOLS = {_descriptor_id(t) for t in _tool_ids_where("cli", "static")}

# Service names produced by the optional registrars (derived from the one table above).
_CLI_OPTIONAL_TOOLS = {svc for _mod, _fn, services in _CLI_OPTIONAL_REGISTRARS for svc in services}

# The CLI capability set — what `rob` CAN register when the relevant flag is on. Derived
# from the two sources above (no independently-maintained flat list). Used by
# cli_unavailable_tools() for honest "tool not available on the CLI" feedback and by the
# startup self-check. Flag-INDEPENDENT (lists a tool even when its flag is currently off,
# so the user gets "enable the flag" rather than "not available"). The one non-derived
# addition: "browser" itself is never a descriptor in get_tool_init_order() (only
# "browser_manager" is — "browser" is the runtime alias the special-case block above
# registers once browser_manager initializes), so without this explicit addition an
# agent requesting tool_ids=["browser"] would get a false "not available on the CLI"
# from cli_unavailable_tools() even though it now actually registers.
_CLI_REGISTERABLE_TOOLS = set(_CLI_STATIC_TOOLS) | set(_CLI_OPTIONAL_TOOLS) | {"browser"}


def cli_unavailable_tools(requested):
    """Return requested tool ids the CLI container cannot provide (for honest UX)."""
    return [t for t in (requested or []) if t not in _CLI_REGISTERABLE_TOOLS]
