"""ONE per-tool capability table (WS-2, 2026-07-16) — the derivation source for the
tool-id gate sets that were previously seven hand-lists co-evolving per tool.

Lives in ``core/`` so every gate module can import it without a layering violation
(``agents/*`` and ``tools/*`` both import downward into core). Dimensions are
ORTHOGONAL — a single "dangerous" bit cannot express the live polarities
(email/twitter/browser are delegable-but-high-impact; hyperliquid/polymarket reads
stay allowed while correspondent-tainted but their trade VERBS gate; x402_pay is
fully blocked):

- ``money``            — can move money (spend or receivables). Safety invariant.
- ``high_impact``      — the WHOLE tool_id is denied while a session is
                          correspondent-tainted (correspondent_gate Layer 2).
- ``delegate_blocked`` — a delegated leaf child never gets this tool_id
                          (default set; ``DELEGATE_BLOCKED_TOOLS`` env still overrides).
- ``exec``             — executes code/commands (docs/telemetry dimension).
- ``shared_identity``  — acts AS the instance (its X account, mailbox, GitHub/HF
                          credentials, public URLs, MCP servers, durable autonomous
                          work). Loaded for the OWNER tenant only
                          (``tools/controller/tool_management.py``, M04): a
                          non-owner tenant (A2A/API caller) never gets it.
- ``writes_*``         — the EXTERNAL EFFECT ceiling (033): which class of outward
                          write the tool is CAPABLE of (social / comms / public /
                          money / code / self / network). A telemetry + pause
                          dimension, not a taint gate. ``core/effects.py`` refines
                          it per action; an unlisted verb on a write-capable tool
                          is assumed to WRITE. ``web_fetch`` and ``perplexity``
                          carry none on purpose: both are read egress.
- ``readable_while_tainted`` — documents the DELIBERATE absence of ``high_impact``
                          on a money venue whose read verbs stay allowed (the trade
                          verbs gate by NAME in correspondent_gate's Tier-B sets).

Per-tool POLICY FIELDS (067 P1) ride on the same rows as attributes of
:class:`ToolRow` (a frozenset subclass, so the capability set is unchanged):
``untrusted_output``, ``cli``, ``cli_registrar``, ``gate``. Views derived from them:
``untrusted_wrap.UNTRUSTED_TOOL_NAMESPACES``; ``bootstrap._CLI_INCOMPATIBLE``,
``_CLI_STATIC_TOOLS``, ``_CLI_OPTIONAL_REGISTRARS``; ``tool_load_report.TOOL_GATE_FLAGS``,
``tool_availability.GATED_TOOL_REGISTRY``.

Derived sets (exact, parity-ratcheted by tests/unit/core/test_tool_capabilities.py):
``MONEY_TOOLS = ids_with("money")``, ``DELEGATE_BLOCKED_TOOLS =
ids_with("delegate_blocked")``, ``HIGH_IMPACT_TOOL_IDS = ids_with("high_impact")``.

Verb-level (action-name) sets are Tier B and stay hand-curated next to their gates
(``_HIGH_IMPACT_NAMES``/``_HIGH_IMPACT_VERB_SUBSTRINGS`` in correspondent_gate, the
approval tuples in tools/controller/approval.py + core/config_policy) — a tool_id
table cannot express per-verb policy; the T12 cross-consistency tests keep them in
sync.

Registration guard: ``tools/descriptors.py::register_optional_tool`` refuses a tool
whose display name is not classified here — a NEW tool cannot silently skip every
gate again. Classify it (an explicit empty ``frozenset()`` means "consciously no
special capabilities"), don't special-case it.
"""
import sys
from typing import Callable, Dict, FrozenSet, Iterable, Optional, Tuple

from core.tool_gates import ToolGate, gate_for, gate_on, register_gate  # noqa: F401  (the gate column)

#: ``ToolRow.cli`` values.
CLI_MODES: FrozenSet[str] = frozenset({"static", "optional", "incompatible", "none"})


class ToolRow(frozenset):
    """One tool's row (067 P1): the capability tokens — the frozenset itself, so
    every existing reader (``in``, ``&``, ``sorted``, ``== frozenset(...)``) keeps
    working unchanged — plus the per-tool POLICY FIELDS that are not capability
    tokens and therefore must not appear in the set:

    - ``untrusted_output`` — results carry attacker-authorable third-party bytes and
      are framed as DATA (derives ``core.security.untrusted_wrap``'s namespaces).
    - ``cli`` — how the lightweight CLI container serves the tool: ``static`` (an
      always-present descriptor), ``optional`` (materialized by ``cli_registrar``,
      self-gating), ``incompatible`` (needs the heavy server container), ``none``
      (never registered on the CLI). Derives ``core.bootstrap``'s ``_CLI_*`` tables.
    - ``cli_registrar`` — ``"module:function"`` of the ``register_optional_tool()``
      wrapper for an ``optional`` row; bootstrap imports it lazily, so core never
      imports the tool tier at module import.
    - ``gate`` — the static :class:`core.tool_gates.ToolGate` (flag + disclosure).
      The live predicate is registered by the tool at import
      (:func:`register_gate`, re-exported here from ``core.tool_gates``).

    A plain ``frozenset()`` row is normalized to a ``ToolRow`` with the defaults at
    import; read a field with :func:`row_field`, which also tolerates a plain
    frozenset patched in by a test.
    """

    __slots__ = ("untrusted_output", "cli", "cli_registrar", "gate")

    def __new__(cls, caps: Iterable[str] = (), *, untrusted_output: bool = False,
                cli: str = "none", cli_registrar: Optional[str] = None,
                gate: Optional[ToolGate] = None):
        if cli not in CLI_MODES:
            raise ValueError(f"cli={cli!r} not in {sorted(CLI_MODES)}")
        if (cli == "optional") != bool(cli_registrar):
            raise ValueError("cli='optional' requires cli_registrar (and only it may have one)")
        row = super().__new__(cls, caps)
        row.untrusted_output = bool(untrusted_output)
        row.cli = cli
        row.cli_registrar = cli_registrar
        row.gate = gate
        return row

    def fields(self) -> Dict[str, object]:
        return {name: getattr(self, name) for name in ToolRow.__slots__}

    def __reduce__(self):
        return (_make_row, (tuple(self), self.fields()))

    def __repr__(self) -> str:
        extra = ", ".join(f"{k}={v!r}" for k, v in self.fields().items()
                          if v != _ROW_DEFAULTS[k])
        body = f"{set(self)!r}" if self else ""
        return f"ToolRow({body}{', ' if body and extra else ''}{extra})"


def _make_row(caps, fields) -> "ToolRow":
    return ToolRow(caps, **fields)


_ROW_DEFAULTS: Dict[str, object] = {
    "untrusted_output": False, "cli": "none", "cli_registrar": None, "gate": None,
}

# tool_id -> capability set. An explicit empty set IS a classification ("read-only /
# workspace-local; consciously none"). ``tool_manage`` is the one ASPIRATIONAL id —
# gated everywhere but not yet registrable (see T12's _ASPIRATIONAL_IDS).
TOOL_CAPABILITIES: Dict[str, FrozenSet[str]] = {
    # -- core / read-only -----------------------------------------------------
    "filesystem": ToolRow(untrusted_output=True, cli="static"),
    "task": ToolRow(cli="static"),   # the TODO tool, NOT delegation — never block
    # No tool-id capability, but its four verbs are gated BY NAME while
    # correspondent-tainted (correspondent_gate._HIGH_IMPACT_NAMES, M03) — the
    # same shape as `memory`/`session_search`.
    "knowledge": ToolRow(
        cli="optional", cli_registrar="tools.knowledge_ingest:register_knowledge_tool",
        gate=ToolGate(flag="KB_ENABLED",
                      tier="disabled",
                      label="KB_ENABLED",
                      remedy="owner sets KB_ENABLED=true")),
    "alchemy": ToolRow(cli="incompatible"),            # read-only chain data
    "collabland": ToolRow(cli="incompatible"),
    # Read-only token sight (proposal 023 T0+T1). No signer, no broadcast, so
    # not `money`. The one own-funds verb (portfolio) is gated by NAME in
    # correspondent_gate — holdings are pre-drain reconnaissance — while the
    # impersonal reads stay available while tainted.
    "defi_data": ToolRow(
        untrusted_output=True, cli="optional", cli_registrar="tools.defi:register_defi_data_tool",
        gate=ToolGate(flag="DEFI_DATA_ENABLED")),
    # -- egress / comms (delegable-but-high-impact: NOT delegate_blocked) ------
    "browser": ToolRow({"high_impact", "writes_network"},  # SSRF / exfil
        untrusted_output=True, cli="static",
        gate=ToolGate(tier="loadable",
                      label="session tool_ids",
                      remedy="request tool_ids=['browser']")),
    "web_fetch": ToolRow({"high_impact"},  # outbound fetch (SSRF / exfil)
        untrusted_output=True, cli="static"),
    # send_email — outbound comms
    "email": ToolRow({"high_impact", "shared_identity", "writes_comms"},
        untrusted_output=True, cli="static",
        gate=ToolGate(tier="disabled",
                      label="SMTP/IMAP credentials + outbound policy",
                      remedy="owner configures email creds; sends follow outbound.policy")),
    # twitter / x_browser: rows in the x pack's pack.toml (067 P3b).
    # 042: token launchpads. MONEY (it signs a spend), high_impact, and
    # delegate_blocked -- a delegated leaf that can launch a token and seed it
    # with the treasury has escaped every bound its parent was operating under.
    "launchpad": frozenset({"money", "high_impact", "delegate_blocked", "writes_money"}),
    # 042: the injected dapp wallet. MONEY because `dapp_connect` authorizes
    # spending -- the transaction itself happens inside a browser callback the
    # Controller never sees, so the CONNECT is the gated act.
    "dapp_browser": frozenset({"money", "high_impact", "delegate_blocked", "writes_money"}),
    # anysite / perplexity: rows in the discovery pack's pack.toml (067 P3a).
    # -- autonomous work ------------------------------------------------------
    "goal": ToolRow({"high_impact", "shared_identity"},  # durable autonomous work (leaf MAY read)
        cli="optional", cli_registrar="tools.goal_tools:register_goal_tool",
        gate=ToolGate(flag="GOALS_ENABLED",
                      tier="disabled",
                      label="GOALS_ENABLED",
                      remedy="owner sets GOALS_ENABLED=true")),
    "cronjob": ToolRow({"high_impact", "delegate_blocked", "shared_identity"},
        cli="optional", cli_registrar="tools.cronjob_tools:register_cronjob_tool",
        gate=ToolGate(flag="CRON_ENABLED",
                      tier="disabled",
                      label="CRON_ENABLED (AUTONOMY_POSTURE>=full)",
                      remedy="owner raises AUTONOMY_POSTURE or sets CRON_ENABLED=true")),
    # -- code / exec / self-modification --------------------------------------
    "code_execution": ToolRow({"high_impact", "delegate_blocked", "exec", "writes_code"},
        cli="optional", cli_registrar="tools.code_exec:register_code_exec_tool",
        gate=ToolGate(flag="CODE_EXEC_ENABLED",
                      tier="disabled",
                      label="CODE_EXEC_ENABLED / AGENT_COMPUTE_POSTURE>=1",
                      remedy=("owner raises AGENT_COMPUTE_POSTURE "
                              "(host axis; not part of AUTONOMY_MODE)"))),
    "coding": ToolRow({"high_impact", "delegate_blocked", "exec", "writes_code"},
        cli="optional", cli_registrar="tools.coding:register_coding_tool",
        gate=ToolGate(flag="CODING_TOOLS_ENABLED",
                      tier="disabled",
                      label="CODING_TOOLS_ENABLED",
                      remedy="owner sets CODING_TOOLS_ENABLED=true")),
    "shell": ToolRow({"high_impact", "delegate_blocked", "exec", "writes_code"},
        cli="optional", cli_registrar="tools.shell:register_shell_tools",
        gate=ToolGate(flag="SHELL_TOOLS_ENABLED",
                      tier="disabled",
                      label="AGENT_COMPUTE_POSTURE>=1",
                      remedy="owner raises AGENT_COMPUTE_POSTURE")),
    "process": ToolRow({"high_impact", "delegate_blocked", "exec", "writes_code"},
        cli="optional", cli_registrar="tools.shell:register_shell_tools",
        gate=ToolGate(flag="SHELL_TOOLS_ENABLED")),
    "self_env": ToolRow({"high_impact", "delegate_blocked", "exec",
                           "writes_code", "writes_self"},
        cli="optional", cli_registrar="tools.self_env:register_self_env_tool",
        gate=ToolGate(flag="SELF_ENV_ENABLED")),
    "git": ToolRow({"high_impact", "delegate_blocked", "writes_public"},
        untrusted_output=True, cli="optional", cli_registrar="tools.git:register_git_tool",
        gate=ToolGate(flag="GIT_TOOLS_ENABLED")),
    "github": ToolRow({"high_impact", "delegate_blocked", "shared_identity", "writes_public"},
        untrusted_output=True, cli="optional", cli_registrar="tools.github:register_github_tool",
        gate=ToolGate(flag="GITHUB_TOOL_ENABLED")),
    # install path + dynamic exec
    "mcp": ToolRow({"high_impact", "delegate_blocked", "shared_identity", "writes_network"},
        untrusted_output=True, cli="static",
        gate=ToolGate(flag="MCP_ENABLED",
                      tier="disabled",
                      label="MCP_ENABLED + config/mcp_config.json",
                      remedy="owner sets MCP_ENABLED=true and configures servers")),
    "hf_deploy": ToolRow({"high_impact", "delegate_blocked", "shared_identity", "writes_public"},
        cli="optional", cli_registrar="tools.hf_deploy:register_hf_deploy_tool",
        gate=ToolGate(flag="HF_DEPLOY_ENABLED")),
    # The ship rail. Outward-facing (a public URL), so delegate_blocked: a leaf
    # child never decides what the world sees under the owner's domain.
    "publish": ToolRow({"high_impact", "delegate_blocked", "shared_identity", "writes_public"},
        cli="optional", cli_registrar="tools.publish:register_publish_tool",
        gate=ToolGate(flag="PUBLISH_ENABLED")),
    # The durable app service (032): a running process behind a public URL. The
    # agent only writes a registry row (the owner-owned supervisor holds the docker/
    # nginx privilege), but a leaf child never decides what the world runs.
    "app_service": ToolRow({"high_impact", "delegate_blocked", "shared_identity",
                             "writes_public"},
        cli="optional", cli_registrar="tools.app_service:register_app_service_tool",
        gate=ToolGate(flag="APP_SERVICE_ENABLED")),
    "tool_manage": frozenset({"high_impact", "delegate_blocked"}),  # aspirational
    # 041 phase 2: a controller ACTION (not a container tool) with a row so the
    # gates derive it — it writes a prompt the orchestrator later reads, so it is
    # denied while correspondent-tainted and never a delegated leaf's to use.
    "worker_manage": frozenset({"high_impact", "delegate_blocked"}),
    # -- money ---------------------------------------------------------------
    "x402_pay": ToolRow({"money", "high_impact", "delegate_blocked",
                           "writes_money", "writes_network"},
        untrusted_output=True, cli="optional", cli_registrar="tools.x402:register_x402_tool",
        gate=ToolGate(tier="reserved",
                      label="money-SPEND",
                      remedy=("owner-only, explicitly enabled, never autonomous; "
                              "raise an ask if truly needed"))),
    "x402_invoice": ToolRow({"money", "high_impact", "delegate_blocked", "writes_money"},
        cli="optional", cli_registrar="tools.x402:register_x402_invoice_tool",
        gate=ToolGate(flag="X402_INVOICE_ENABLED",
                      tier="disabled",
                      label="X402_INVOICE_ENABLED",
                      remedy="owner sets X402_INVOICE_ENABLED=true (auto-ON in autonomous mode)")),
    # On-chain money verbs (proposal 023 T3). Every call routes through
    # core/wallet/tx_guard.py; `money` also makes it explicit-grant-only, so the
    # agent can never self-serve it via load_tool.
    "defi_trade": ToolRow({"money", "high_impact", "delegate_blocked", "writes_money"},
        gate=ToolGate(flag="DEFI_TRADE_ENABLED")),
    # 050/069: agent NFTs (an ERC-6551 token-bound account of an NFT the treasury owns). Its package
    # verbs live in the optional agent-NFT package (AGENT_NFT_PACKAGE_MODULES) and register via register_optional_tool, which
    # refuses an unclassified tool — so the row lands in core BEFORE the package.
    # Every write goes through tx_guard with `via_account`.
    "agent_nft": frozenset({"money", "high_impact", "delegate_blocked", "writes_money"}),
    # Trading venues (hyperliquid, polymarket and their *_data read tools): the
    # markets pack classifies them in its pack.toml (067 P4).
}

# Every row is a ToolRow (a plain frozenset() above means "all field defaults").
TOOL_CAPABILITIES = {tid: row if isinstance(row, ToolRow) else ToolRow(row)
                     for tid, row in TOOL_CAPABILITIES.items()}


# --- Late rows (067 P2) ---------------------------------------------------------
# The derived sets below are snapshots in their consumers (built at the
# consumer's import, or on first read where the consumer made them lazy —
# ``core/lazy_views.py``, e.g. ``untrusted_wrap.UNTRUSTED_TOOL_NAMESPACES``). Every
# query records the view it built (keyed by call site); a row registered later
# that the view would include is refused with that view's module, so a late row
# is never silently missing from a gate. Pack rows register in the loader's
# phase 1, at process entry, before these views exist.
_BUILT_VIEWS: Dict[Tuple[str, int], Tuple[str, Callable[[str, FrozenSet[str]], bool]]] = {}
_SOURCES: Dict[str, str] = {}


def _note_view(pred: Callable[[str, FrozenSet[str]], bool], depth: int = 2) -> None:
    frame = sys._getframe(depth)
    _BUILT_VIEWS[(frame.f_code.co_filename, frame.f_lineno)] = (
        frame.f_globals.get("__name__", frame.f_code.co_filename), pred)


def validate_tool_row(tool_id: str, row: "ToolRow", *, source: str) -> None:
    """Raise ``ValueError`` when *row* cannot be added for *tool_id*: the id is
    already classified, collides with a classified id's action namespace
    (``<id>_``) of ANOTHER owner, or the row belongs to a view already built.

    One source may nest its own ids (the markets pack's ``polymarket`` and
    ``polymarket_data``, 067 P4): every action of both carries a row naming its
    tool, and the loader refuses a verb of the shorter id that lies in the
    longer id's namespace."""
    if not isinstance(row, ToolRow):
        raise ValueError(f"{source}: the row for {tool_id!r} is not a ToolRow")
    if not tool_id or tool_id != tool_id.strip():
        raise ValueError(f"{source}: bad tool id {tool_id!r}")
    if tool_id in TOOL_CAPABILITIES:
        owner = _SOURCES.get(tool_id, "core")
        raise ValueError(f"{source}: tool {tool_id!r} is already classified by {owner}")
    for other in TOOL_CAPABILITIES:
        if _SOURCES.get(other) == source:
            continue
        if tool_id.startswith(other + "_") or other.startswith(tool_id + "_"):
            raise ValueError(f"{source}: tool {tool_id!r} shares the action namespace "
                             f"of {other!r}")
    for reader, pred in _BUILT_VIEWS.values():
        if pred(tool_id, row):
            raise ValueError(f"{source}: tool {tool_id!r} would change a capability view "
                             f"{reader} already built; tool rows register before that "
                             "view is imported (067 P2 phase 1)")


def register_tool_row(tool_id: str, row: "ToolRow", *, source: str) -> None:
    """Classify a tool contributed from outside this table (a pack, 067 P2)."""
    validate_tool_row(tool_id, row, source=source)
    TOOL_CAPABILITIES[tool_id] = row
    _SOURCES[tool_id] = source


def classified_ids() -> FrozenSet[str]:
    """Every classified tool id — for a consumer that snapshots the whole table."""
    _note_view(lambda _t, _r: True)
    return frozenset(TOOL_CAPABILITIES)


def row_field(tool_id: str, name: str):
    """The policy field *name* of *tool_id*'s row, or its default when the tool has
    no row (or a test patched in a plain frozenset)."""
    return getattr(TOOL_CAPABILITIES.get(tool_id), name, _ROW_DEFAULTS[name])


def ids_where(name: str, value=True) -> FrozenSet[str]:
    """All tool_ids whose policy field *name* equals *value* — the derivation the
    per-tool views (untrusted namespaces, CLI tables, gate maps) are built from."""
    _note_view(lambda _t, row: getattr(row, name, _ROW_DEFAULTS[name]) == value)
    return frozenset(t for t in TOOL_CAPABILITIES if row_field(t, name) == value)


# --- The gate column's two views ---------------------------------------------------
# They DISAGREE on membership today (067 run 2), and each view keeps its exact
# pre-067 contents — this task does not unify their semantics:
#   flag only (never disclosed): publish, app_service, hf_deploy, github, git, process,
#       self_env, defi_trade, defi_data;
#   disclosed only (no flag for the load report): twitter, email, browser, x402_pay,
#       hyperliquid, polymarket (twitter's label names TWITTER_ENABLED, yet the load
#       report does not);
#   both: knowledge, goal, cronjob, code_execution, coding, shell, x_browser, mcp,
#       x402_invoice.
# Known wording debt, kept verbatim: the reserved remedies say "never autonomous",
# which contradicts the x402 micro lane and DEFI_AGENT_AUTONOMY.


def gate_flag_map() -> Dict[str, str]:
    """``tool_id -> env flag`` for every row whose gate names a flag
    (``tools.controller.tool_load_report.TOOL_GATE_FLAGS``)."""
    _note_view(lambda _t, row: getattr(row, "gate", None) is not None and bool(row.gate.flag))
    return {t: row_field(t, "gate").flag for t in TOOL_CAPABILITIES
            if row_field(t, "gate") is not None and row_field(t, "gate").flag}


def gate_disclosure_map() -> Dict[str, tuple]:
    """``tool_id -> (label, tier, remedy)`` for every disclosed gate
    (``agents.task.agent.core.tool_availability.GATED_TOOL_REGISTRY``)."""
    _note_view(lambda _t, row: getattr(row, "gate", None) is not None and bool(row.gate.tier))
    return {t: row_field(t, "gate").disclosure() for t in TOOL_CAPABILITIES
            if row_field(t, "gate") is not None and row_field(t, "gate").tier}


#: 033: the EXTERNAL EFFECT ceiling tokens. ``core/effects.py`` maps each to one
#: effect class; the action name refines it there.
EFFECT_CAPABILITIES: FrozenSet[str] = frozenset({
    "writes_social", "writes_comms", "writes_public",
    "writes_money", "writes_code", "writes_self", "writes_network",
})

KNOWN_CAPABILITIES: FrozenSet[str] = frozenset({
    "money", "high_impact", "delegate_blocked", "exec", "readable_while_tainted",
    "shared_identity",
}) | EFFECT_CAPABILITIES


def ids_with(capability: str) -> FrozenSet[str]:
    """All tool_ids carrying *capability*. The derivation the gate sets are built from."""
    _note_view(lambda _t, row: capability in row)
    return frozenset(t for t, caps in TOOL_CAPABILITIES.items() if capability in caps)


def is_classified(tool_id: str) -> bool:
    """Whether *tool_id* has an explicit capability row (empty set counts)."""
    return tool_id in TOOL_CAPABILITIES


# --- Product-catalog metadata (folded from core/tool_catalog.py, WS-2 tail) ---------
# Coarse audit/display permission classes for the STATIC descriptor tools. Keyed by
# DESCRIPTOR name — "browser_manager" is the descriptor id whose capability row above
# is "browser" (CATALOG_ALIASES maps it). Product/audit metadata consumed by the
# catalog (webview/API), NOT a gate input; housed here so a tool is classified in ONE
# module instead of a second hand-table drifting in core/tool_catalog.py.
TOOL_PERMISSIONS: Dict[str, Tuple[str, ...]] = {
    "filesystem": ("fs.read", "fs.write"),
    "task": ("memory.read", "memory.write"),
    "browser_manager": ("browser.control", "network.read"),
    "launchpad": ("network.write", "wallet.spend"),
    "dapp_browser": ("network.write", "wallet.spend"),
    "email": ("network.write", "email.send"),
    "collabland": ("network.read",),
    "alchemy": ("network.read",),
    "mcp": ("mcp.call", "network.read"),
    # -- F44/A9 (2026-09-14): the 19 ids that were classified in
    # TOOL_CAPABILITIES but had no TOOL_PERMISSIONS row, so they were invisible
    # to `high_risk_tool_ids()`/`medium_risk_tool_ids()` and to the future
    # Capabilities tab. Tiered by capability set: `money`/`exec` -> at least one
    # external-write permission (lands `high_risk_tool_ids()`, since `exec` means
    # "executes code/commands" -- an unbounded capability that can just as
    # easily reach the network as read a file); `high_impact`-only -> no
    # external-write permission (lands `medium_risk_tool_ids()`); the two
    # empty-capability reads (`knowledge`, `defi_data`) fall through to the
    # dataclass default `"low"`. Mechanism unchanged — only rows added.
    # money/exec -> high (an external-write permission is present on each row):
    "code_execution": ("process.spawn", "fs.read", "fs.write", "network.write"),
    "coding": ("fs.read", "fs.write", "process.spawn", "network.write"),
    "shell": ("process.spawn", "fs.read", "fs.write", "network.write"),
    "process": ("process.spawn", "network.write"),
    "self_env": ("fs.read", "fs.write", "process.spawn", "network.write"),
    "x402_pay": ("network.write", "wallet.spend"),
    # Receivables (invoicing), not agent-initiated spend -- no wallet.spend.
    "x402_invoice": ("network.write", "memory.read", "memory.write"),
    "defi_trade": ("network.write", "wallet.spend"),
    "agent_nft": ("network.write", "wallet.spend"),
    # high_impact-only -> medium (no external-write permission on these rows;
    # `network.read` mirrors the existing browser_manager/mcp/anysite/perplexity
    # precedent of tagging outward reach without the definitive write token):
    "goal": ("memory.read", "memory.write"),
    "cronjob": ("memory.read", "memory.write"),
    "git": ("fs.read", "fs.write"),
    "github": ("network.read",),
    "hf_deploy": ("network.read", "fs.read"),
    "publish": ("network.read", "fs.read"),
    "app_service": ("network.read",),
    "tool_manage": ("mcp.call",),
    "worker_manage": ("memory.read", "memory.write"),
    "web_fetch": ("network.read",),
    # empty capability set -> low (no high_impact, no external-write permission):
    "knowledge": ("fs.read", "memory.read", "memory.write"),
    "defi_data": ("network.read",),
}

#: The permission classes a catalog row may hold (the tiers below read them).
KNOWN_PERMISSIONS: FrozenSet[str] = frozenset(
    p for perms in TOOL_PERMISSIONS.values() for p in perms) | {
    # 067 P3b: carried only by pack tools today (the X pack's twitter/x_browser);
    # still a core class — the risk tiers below read it.
    "social.post",
    # 067 P4: carried only by the markets pack's venue tools (polymarket,
    # hyperliquid); a core class the high-risk tier reads.
    "trade.execute",
}


def validate_tool_permissions(tool_id: str, perms: Iterable[str], *, source: str) -> None:
    """Raise ``ValueError`` when *perms* cannot become *tool_id*'s catalog row
    (067 P2 pack contribution): the row exists, or a class is unknown."""
    perms = tuple(perms)
    if tool_id in TOOL_PERMISSIONS:
        raise ValueError(f"{source}: tool {tool_id!r} already has a catalog permission row")
    unknown = sorted(set(perms) - KNOWN_PERMISSIONS)
    if unknown:
        raise ValueError(f"{source}: tool {tool_id!r}: unknown permission class(es) {unknown} "
                         f"(known: {sorted(KNOWN_PERMISSIONS)})")


def register_tool_permissions(tool_id: str, perms: Iterable[str], *, source: str) -> None:
    """Add a pack tool's catalog permission row (read at call time by the risk tiers)."""
    perms = tuple(perms)
    validate_tool_permissions(tool_id, perms, source=source)
    TOOL_PERMISSIONS[tool_id] = perms


# Descriptor/display id -> capability-table id (the one naming dual).
CATALOG_ALIASES: Dict[str, str] = {"browser_manager": "browser"}


def descriptor_id(tool_id: str) -> str:
    """Capability-table id -> the tool DESCRIPTOR id (inverse of CATALOG_ALIASES)."""
    for desc, cap in CATALOG_ALIASES.items():
        if cap == tool_id:
            return desc
    return tool_id

# Permissions with an irreversible EXTERNAL write side effect — the catalog's
# "high risk" tier derives from these (posting, sending, trading).
_EXTERNAL_WRITE_PERMISSIONS: FrozenSet[str] = frozenset({
    "network.write", "social.post", "email.send", "trade.execute",
})


def high_risk_tool_ids() -> FrozenSet[str]:
    """Catalog tools whose permissions include an external write side effect."""
    return frozenset(
        t for t, perms in TOOL_PERMISSIONS.items()
        if _EXTERNAL_WRITE_PERMISSIONS.intersection(perms)
    )


def medium_risk_tool_ids() -> FrozenSet[str]:
    """Catalog tools that are ``high_impact`` (via their capability row) but carry no
    external-write permission — dynamic egress/exec surfaces (browser/mcp/anysite/
    perplexity), riskier than read-only but below posting/sending/trading."""
    high = high_risk_tool_ids()
    return frozenset(
        t for t in TOOL_PERMISSIONS
        if t not in high
        and "high_impact" in TOOL_CAPABILITIES.get(CATALOG_ALIASES.get(t, t), frozenset())
    )


def lacks_effect_ceiling(tool_id: str) -> bool:
    """033: a tool that can move money, execute code, or holds an external-write
    catalog permission, yet carries no ``writes_*`` token. Such a tool would be
    invisible to the effect recorder and the pause gate; the registration guard
    (``tools/descriptors.py::register_optional_tool``) refuses it."""
    tid = CATALOG_ALIASES.get(tool_id, tool_id)
    caps = TOOL_CAPABILITIES.get(tid, frozenset())
    write_capable = bool(caps & {"money", "exec"}) or tool_id in high_risk_tool_ids()
    return write_capable and not (caps & EFFECT_CAPABILITIES)


#: The optional agent-NFT package, by import name, in preference order. ``polyrob_drop`` is
#: the current name; the second entry is the pre-rename fallback, read until the package
#: renames (069 v4).
AGENT_NFT_PACKAGE_MODULES = ("polyrob_drop", "polyrob_desk")
