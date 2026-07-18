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
- ``readable_while_tainted`` — documents the DELIBERATE absence of ``high_impact``
                          on a money venue whose read verbs stay allowed (the trade
                          verbs gate by NAME in correspondent_gate's Tier-B sets).

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
from typing import Dict, FrozenSet, Tuple

# tool_id -> capability set. An explicit empty set IS a classification ("read-only /
# workspace-local; consciously none"). ``tool_manage`` is the one ASPIRATIONAL id —
# gated everywhere but not yet registrable (see T12's _ASPIRATIONAL_IDS).
TOOL_CAPABILITIES: Dict[str, FrozenSet[str]] = {
    # -- core / read-only -----------------------------------------------------
    "filesystem": frozenset(),
    "task": frozenset(),               # the TODO tool, NOT delegation — never block
    "knowledge": frozenset(),
    "alchemy": frozenset(),            # read-only chain data
    "collabland": frozenset(),
    "polymarket_data": frozenset(),    # read-only market data (no wallet)
    "hyperliquid_data": frozenset(),   # read-only market data (no wallet)
    # -- egress / comms (delegable-but-high-impact: NOT delegate_blocked) ------
    "browser": frozenset({"high_impact"}),        # SSRF / exfil
    "web_fetch": frozenset({"high_impact"}),      # outbound fetch (SSRF / exfil)
    "email": frozenset({"high_impact"}),          # send_email — outbound comms
    "twitter": frozenset({"high_impact"}),        # post/reply/quote — outbound comms
    "anysite": frozenset({"high_impact"}),        # outbound structured-data egress
    "perplexity": frozenset({"high_impact"}),     # outbound search egress
    # -- autonomous work ------------------------------------------------------
    "goal": frozenset({"high_impact"}),           # durable autonomous work (leaf MAY read)
    "cronjob": frozenset({"high_impact", "delegate_blocked"}),
    # -- code / exec / self-modification --------------------------------------
    "code_execution": frozenset({"high_impact", "delegate_blocked", "exec"}),
    "coding": frozenset({"high_impact", "delegate_blocked", "exec"}),
    "shell": frozenset({"high_impact", "delegate_blocked", "exec"}),
    "process": frozenset({"high_impact", "delegate_blocked", "exec"}),
    "self_env": frozenset({"high_impact", "delegate_blocked", "exec"}),
    "git": frozenset({"high_impact", "delegate_blocked"}),
    "github": frozenset({"high_impact", "delegate_blocked"}),
    "mcp": frozenset({"high_impact", "delegate_blocked"}),   # install path + dynamic exec
    "hf_deploy": frozenset({"high_impact", "delegate_blocked"}),
    "tool_manage": frozenset({"high_impact", "delegate_blocked"}),  # aspirational
    # -- money ---------------------------------------------------------------
    "x402_pay": frozenset({"money", "high_impact", "delegate_blocked"}),
    "x402_invoice": frozenset({"money", "high_impact", "delegate_blocked"}),
    # Trading venues: reads stay allowed while tainted (deliberately NOT high_impact
    # as a tool_id); the trade verbs gate by name in correspondent_gate Tier B.
    "hyperliquid": frozenset({"money", "delegate_blocked", "readable_while_tainted"}),
    "polymarket": frozenset({"money", "delegate_blocked", "readable_while_tainted"}),
}

KNOWN_CAPABILITIES: FrozenSet[str] = frozenset({
    "money", "high_impact", "delegate_blocked", "exec", "readable_while_tainted",
})


def ids_with(capability: str) -> FrozenSet[str]:
    """All tool_ids carrying *capability*. The derivation the gate sets are built from."""
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
    "perplexity": ("network.read",),
    "twitter": ("network.read", "network.write", "social.post"),
    "email": ("network.write", "email.send"),
    "collabland": ("network.read",),
    "alchemy": ("network.read",),
    "mcp": ("mcp.call", "network.read"),
    "anysite": ("network.read", "process.spawn"),
    "polymarket": ("network.read", "trade.execute"),
    "hyperliquid": ("network.read", "trade.execute"),
    # Read-only split tools: market data only, no wallet/signing.
    "polymarket_data": ("network.read",),
    "hyperliquid_data": ("network.read",),
}

# Descriptor/display id -> capability-table id (the one naming dual).
CATALOG_ALIASES: Dict[str, str] = {"browser_manager": "browser"}

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
