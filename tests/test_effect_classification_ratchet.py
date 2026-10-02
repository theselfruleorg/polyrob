"""033 ratchet — every external writer routes through the ONE effect recorder.

`social_write` was not a class of event: it was one hand-written `record()` call
inside one tool (the twitter tool, now packs/x/polyrob_x/twitter_tool.py), reachable from two of that tool's
twenty-two actions. In fourteen days of production it wrote 25 rows while eight
public Telegram broadcast posts wrote none. Every reader was a closed allowlist
that a new writer walked past.

Proposal 033 replaces that with one effect classification (``core/effects.py``)
recorded at three seams:

1. the Controller post-hook (``tools/controller/effect_hooks.py``) — every
   agent-driven action, so a tool module whose writes are Controller actions is
   covered by the seam, not by a call of its own (``CONTROLLER_SEAM``);
2. the two ``MessageRouter`` methods — every surface send that is not an action
   (``ROUTER_SEAM``);
3. a direct ``core.effects.record_external_write`` call from a writer with
   neither (cron delivery, the app supervisor, the settlement notifier).

What this file pins, in both directions (mirroring tests/test_layering_ratchet.py):

- ``UNCOVERED_WRITERS`` is the frozen list of modules that perform an external
  write WITHOUT reaching a seam. It is SHRINK-ONLY, and a row whose module now
  reaches the recorder must be DELETED in the same commit.
- Coverage: every write-capable catalog tool carries a ``writes_*`` ceiling.
- Vocabulary: every token and every row names a known effect class.
- No stale rows: every classified verb is a real registered action name.
"""
import ast
import importlib
import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The recorder every external writer must eventually reach.
RECORDER = "record_external_write"

#: The seven effect classes proposal 033 defines. A literal on purpose: this test
#: must fail loudly if core/effects.py renames one.
EFFECT_CLASSES = frozenset({
    "social", "comms", "public", "money", "code", "self", "network",
})

#: module -> the effect class it writes. SHRINK-ONLY: delete a row the day its
#: module reaches the recorder. Empty since 2026-09-23 — keep it that way: a new
#: writer routes through a seam below instead of landing here.
UNCOVERED_WRITERS: dict = {}

#: Writers with neither a Controller nor the router: each calls the recorder
#: itself. Pinned so a refactor cannot silently drop the call.
DIRECT_SEAM = {
    "cron/delivery.py": "cron twitter/email delivery calls the gated ACTION outside a Controller",
    "core/app_service/supervisor.py": "going live behind a public URL",
    "core/surfaces/message_router.py": "publish + send_message_ex (the router seam itself)",
}

#: Modules whose sends reach a person only through the router seam, which
#: records at delivery (direct send) or at durable acceptance (the queue): the
#: dispatcher drains rows the router already recorded, and a surface's
#: ``send`` is called only by the router, the dispatcher or its own stream
#: finalizer. ``test_no_direct_surface_send`` pins that.
ROUTER_SEAM = (
    "core/surfaces/outbound_dispatcher.py",
    "surfaces/telegram/surface.py",
    "surfaces/email/surface.py",
    "surfaces/discord/surface.py",
    "surfaces/slack/surface.py",
    "surfaces/signal/surface.py",
    "surfaces/whatsapp/surface.py",
    "packs/x/polyrob_x/surface/surface.py",  # the X pack's DM surface
)

#: The only modules allowed to call ``<surface>.send(OutboundMessage(...))``.
_SURFACE_SEND_ALLOWED = frozenset({
    "core/surfaces/message_router.py",
    "core/surfaces/outbound_dispatcher.py",
    "core/surfaces/surface.py",          # the base stream() finalizer
})

#: module -> the tool_id (or the tool-less action) the Controller post-hook
#: classifies its writes under. Every write these modules make is a Controller
#: action, so the ONE hook records it — success and failure alike.
CONTROLLER_SEAM = {
    "packs/x/polyrob_x/twitter_tool.py": "twitter",
    "packs/x/polyrob_x/x_browser/tool.py": "x_browser",
    "tools/publish/tool.py": "publish",
    "tools/git/tool.py": "git",
    "tools/github/tool.py": "github",
    "tools/email_tool.py": "email",
    "tools/mcp/mcp_tool.py": "mcp",
    "packs/discovery/polyrob_discovery/anysite/tool.py": "anysite",
    "tools/browser/browser.py": "browser",
    "tools/shell/tool.py": "shell",
    "tools/shell/process_tool.py": "process",
    "tools/code_exec/tool.py": "code_execution",
    "packs/markets/polyrob_markets/hyperliquid/service.py": "hyperliquid",
    "packs/markets/polyrob_markets/polymarket/service.py": "polymarket",
    # tool-less closures: classified by ACTION_EFFECTS, not by a ceiling
    "tools/controller/message_send.py": "action:message",
    "tools/tool_disclosure.py": "action:load_tool",
}

#: tool_id -> the module whose decorated methods are its registered actions.
#: Used to prove every classified verb is REAL (a stale row silences nothing
#: but misleads every reader of core/effects.py).
TOOL_MODULES = {
    "twitter": "polyrob_x.twitter_tool", "email": "tools.email_tool",
    "x_browser": "polyrob_x.x_browser.tool", "publish": "tools.publish.tool",
    "git": "tools.git.tool", "github": "tools.github.tool",
    "hf_deploy": "tools.hf_deploy.tool", "app_service": "tools.app_service.tool",
    "mcp": "tools.mcp.mcp_tool", "anysite": "polyrob_discovery.anysite.tool",
    "shell": "tools.shell.tool", "process": "tools.shell.process_tool",
    "code_execution": "tools.code_exec.tool", "coding": "tools.coding.tool",
    "self_env": "tools.self_env.tool", "x402_pay": "tools.x402.service",
    "x402_invoice": "tools.x402.invoice_tool", "defi_trade": "tools.defi.trade_tool",
    "hyperliquid": "polyrob_markets.hyperliquid.service",
    "polymarket": "polyrob_markets.polymarket.service",
    "launchpad": "tools.launchpad.tool", "dapp_browser": "tools.dapp_browser.tool",
    "browser": "tools.browser.browser", "agent_nft": "tools.agent_nft.tool",
}


#: Tools whose verbs register from an OPTIONAL package outside this tree, so
#: their classified verbs cannot be proven real here. Each row needs a reason.
OPTIONAL_PACKAGE_TOOLS: frozenset = frozenset()


def _reaches_recorder(path: Path) -> bool:
    """True when the module calls, or imports, ``record_external_write``.

    AST rather than a substring so a mention in a docstring or a comment does
    not count as coverage.
    """
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "attr", None) or getattr(fn, "id", None)) == RECORDER:
                return True
        if isinstance(node, ast.ImportFrom) and node.module == "core.effects":
            if any(a.name == RECORDER for a in node.names):
                return True
    return False


def _registered_actions(tool_id: str) -> set:
    """The registered names of *tool_id*'s decorated action methods (the same
    namespacing rule ``Controller.add_tool`` applies)."""
    from tools.base_tool import BaseTool
    mod = importlib.import_module(TOOL_MODULES[tool_id])
    names = set()
    for _, cls in inspect.getmembers(mod, inspect.isclass):
        if not (issubclass(cls, BaseTool) and cls is not BaseTool
                and cls.__module__ == mod.__name__):
            continue
        for n, f in inspect.getmembers(cls):
            if n.startswith("_") or not (hasattr(f, "_description") or hasattr(f, "action_info")):
                continue
            names.add(n if (n == tool_id or n.startswith(tool_id + "_")) else f"{tool_id}_{n}")
    return names


# --- the debt list ---------------------------------------------------------------

def test_uncovered_writer_rows_still_exist():
    missing = sorted(m for m in {**UNCOVERED_WRITERS, **CONTROLLER_SEAM,
                                 **DIRECT_SEAM, **dict.fromkeys(ROUTER_SEAM)}
                     if not (REPO / m).exists())
    assert missing == [], f"stale ratchet rows (module gone): {missing}"


def test_uncovered_writers_only_shrink():
    fixed = sorted(m for m in UNCOVERED_WRITERS if _reaches_recorder(REPO / m))
    assert fixed == [], (
        "these modules now reach record_external_write — delete their "
        f"UNCOVERED_WRITERS rows in the same commit: {fixed}")


def test_every_declared_effect_is_a_known_class():
    bad = {m: e for m, e in UNCOVERED_WRITERS.items() if e not in EFFECT_CLASSES}
    assert bad == {}, f"unknown effect classes in UNCOVERED_WRITERS: {bad}"


def test_the_debt_is_not_silently_growing():
    """Raising this number is a deliberate act that shows up in review."""
    assert len(UNCOVERED_WRITERS) <= 0, (
        f"UNCOVERED_WRITERS grew to {len(UNCOVERED_WRITERS)} — a NEW external "
        "writer was added without routing through core.effects.record_external_write")


# --- the direct and router seams ---------------------------------------------------

def test_every_direct_seam_module_reaches_the_recorder():
    missing = sorted(m for m in DIRECT_SEAM if not _reaches_recorder(REPO / m))
    assert missing == [], f"direct-seam writers that no longer record: {missing}"


def test_no_direct_surface_send():
    """A surface's send() outside the router seam would be an unrecorded write."""
    import subprocess
    out = subprocess.run(["git", "ls-files", "--", "core", "modules", "agents", "tools",
                          "api", "cli", "surfaces", "webview", "cron"],
                         cwd=REPO, capture_output=True, text=True, timeout=30).stdout
    offenders = []
    for rel in out.split():
        if not rel.endswith(".py") or "/tests/" in f"/{rel}" or rel in _SURFACE_SEND_ALLOWED:
            continue
        src = (REPO / rel).read_text(errors="replace")
        if ".send(OutboundMessage(" in src:
            offenders.append(rel)
    assert offenders == [], (
        "a surface is sent to directly, outside core/surfaces/message_router.py — "
        f"route it through the router (033 seam): {offenders}")


# --- the Controller seam ---------------------------------------------------------

def test_the_controller_registers_both_effect_hooks():
    src = (REPO / "tools/controller/service.py").read_text()
    assert "make_effect_record_hook(self)" in src
    assert "make_effect_gate_hook(self)" in src
    assert _reaches_recorder(REPO / "tools/controller/effect_hooks.py")


def test_every_controller_seam_module_is_classified():
    """A module covered by the hook is only covered if the hook can CLASSIFY it."""
    from core.effects import ACTION_EFFECTS, ceiling
    bad = {}
    for mod, owner in CONTROLLER_SEAM.items():
        if owner.startswith("action:"):
            if not ACTION_EFFECTS.get(owner.split(":", 1)[1]):
                bad[mod] = owner
        elif ceiling(owner) is None:
            bad[mod] = owner
    assert bad == {}, f"controller-seam modules the hook cannot classify: {bad}"


# --- coverage / vocabulary / stale rows -------------------------------------------

def test_every_write_capable_tool_declares_an_effect():
    from core.tool_capabilities import (CATALOG_ALIASES, EFFECT_CAPABILITIES,
                                        TOOL_CAPABILITIES, high_risk_tool_ids)
    missing = sorted(
        CATALOG_ALIASES.get(t, t) for t in high_risk_tool_ids()
        if not (TOOL_CAPABILITIES.get(CATALOG_ALIASES.get(t, t), frozenset())
                & EFFECT_CAPABILITIES))
    assert missing == [], f"external-write tools with no writes_* token: {missing}"


def test_vocabulary_is_closed():
    from core.effects import (ACTION_EFFECTS, EFFECT_CLASSES as CORE_CLASSES,
                              TOKEN_TO_EFFECT, WRITE_ACTIONS)
    from core.tool_capabilities import EFFECT_CAPABILITIES
    assert CORE_CLASSES == EFFECT_CLASSES
    assert set(TOKEN_TO_EFFECT) == set(EFFECT_CAPABILITIES)
    assert set(TOKEN_TO_EFFECT.values()) == EFFECT_CLASSES
    bad = {(t, a, e) for t, rows in WRITE_ACTIONS.items() for a, e in rows.items()
           if e not in EFFECT_CLASSES}
    bad |= {("", a, e) for a, e in ACTION_EFFECTS.items() if e and e not in EFFECT_CLASSES}
    assert not bad, f"rows naming an unknown effect class: {sorted(bad)}"


def test_classified_tools_are_write_capable_and_rows_disjoint():
    from core.effects import NON_WRITE_ACTIONS, WRITE_ACTIONS, ceiling
    stale = sorted(t for t in {*NON_WRITE_ACTIONS, *WRITE_ACTIONS} if ceiling(t) is None)
    assert stale == [], f"rows for a tool with no writes_* ceiling: {stale}"
    both = sorted((t, a) for t, rows in WRITE_ACTIONS.items()
                  for a in rows if a in NON_WRITE_ACTIONS.get(t, frozenset()))
    assert both == [], f"verbs listed as BOTH a write and a non-write: {both}"


def test_every_classified_verb_is_a_real_action():
    from core.effects import NON_WRITE_ACTIONS, WRITE_ACTIONS
    missing = {}
    for tid in sorted({*NON_WRITE_ACTIONS, *WRITE_ACTIONS}):
        if tid in OPTIONAL_PACKAGE_TOOLS:
            continue  # its actions live in a package this tree does not ship
        assert tid in TOOL_MODULES, (
            f"add {tid!r} to TOOL_MODULES (its action module), or to "
            "OPTIONAL_PACKAGE_TOOLS when its verbs ship in an optional package")
        real = _registered_actions(tid)
        listed = set(NON_WRITE_ACTIONS.get(tid, ())) | set(WRITE_ACTIONS.get(tid, {}))
        gone = sorted(listed - real)
        if gone:
            missing[tid] = gone
    assert missing == {}, f"classified verbs that are not registered actions: {missing}"


#: Emitted verbs on a write-capable tool that deliberately have NO effect row
#: and write at the tool's ceiling (``confidence="tool"``). SHRINK-ONLY: classify
#: a verb in core/verb_policy_rows.py and delete it here in the same commit.
INHERIT_BY_DESIGN = frozenset({
    "anysite_describe",
    "mcp_connect_server", "mcp_disconnect_server", "mcp_reload_server",
    "mcp_subscribe_resource", "mcp_unsubscribe_resource",
    "twitter_poll_results",
    "x_browser_x_dm", "x_browser_x_read_dms", "x_browser_x_reply",
})


def test_every_write_tool_verb_has_a_policy_row_or_inherits_by_design():
    """067 P1: an emitted verb of a write-capable tool has an explicit effect row
    in the verb-policy table, or is pinned above as a ceiling verb."""
    from core.effects import ceiling
    from core.verb_policy import VERB_POLICY

    unclassified = {}
    for tid in sorted(TOOL_MODULES):
        if ceiling(tid) is None or tid in OPTIONAL_PACKAGE_TOOLS:
            continue
        gap = sorted(n for n in _registered_actions(tid)
                     if (n not in VERB_POLICY or VERB_POLICY[n].effect == "inherit")
                     and n not in INHERIT_BY_DESIGN)
        if gap:
            unclassified[tid] = gap
    assert unclassified == {}, (
        f"write-tool verbs with no effect row: {unclassified}. Add a row in "
        "core/verb_policy_rows.py (effect class, or none for a read).")
    stale = sorted(n for n in INHERIT_BY_DESIGN
                   if n in VERB_POLICY and VERB_POLICY[n].effect != "inherit")
    assert stale == [], f"classified now — delete from INHERIT_BY_DESIGN: {stale}"
