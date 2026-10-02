"""Named tool profiles — the ONE place a named set of tool ids is spelled (067 P1).

Before this module seven literal lists each spelled their own set (the
per-surface defaults, the named ``TOOLSETS``, the autonomous grant and its defi /
agent_nft extensions, the autonomous ``RIGS``, the self-goal ceiling and baseline,
the child-goal inheritance set), and two of them COLLIDED on names with different
content: ``TOOLSETS["research"]`` is not ``RIGS["research"]``. Here every set has
ONE namespaced name:

* ``default:<surface>`` — what a session gets when it asked for nothing;
* ``toolset:<name>``    — the operator-chosen ``POLYROB_AGENT_TOOLSET`` vocabulary;
* ``grant:<name>``      — the autonomous grant and its armed extensions;
* ``rig:<name>``        — the named rigs of an autonomous (cron / goal) run;
* ``ceiling:<name>``    — the most an agent may grant itself;
* ``baseline:<name>`` / ``inherit:<name>`` / ``exclude:<name>`` — the small
  sets the goal rails union, inherit or subtract.

**Order is contract.** Each profile is an ordered tuple; the emitted tool-schema
bytes ride the prompt-cache prefix, so the order here IS the order a session's
tools are offered in. The public names (``TOOLSETS``, ``RIGS``,
``AUTONOMOUS_MODE_TOOLS``, ``server_default_tools()`` …) are VIEWS over this
table with their historical type (list / tuple / frozenset). Regime logic —
autonomy mode, ``DEFI_AGENT_AUTONOMY``, the agent-NFT package probe, compute posture —
stays with its owner and reads the views; this module holds data and a tiny
resolver only (``core`` may not import ``agents``/``tools``).

``message`` is a controller ACTION id, not a container tool id (055 §2.4); it
appears where the rigs and the self-goal ceiling have always listed it.

**Pack members (067 P3).** A profile may name a tool that a pack provides
(``anysite``/``perplexity`` from the discovery pack). :func:`profile` returns only
the members THIS install provides — a classified tool (``core.tool_capabilities``;
a pack's rows register in the loader's phase 1) or a controller action id — so
an absent pack's ids drop out of every view quietly, order kept. The raw names
stay in :data:`PROFILES`.

Extension point (packs, 067 P2+): a pack contributes members by name before the
views are read. The views are import-time snapshots today, so a pack that must
widen one registers into :data:`PROFILES` at import of its registrar, BEFORE
``agents.task.tool_defaults`` / ``agents.task.constants`` / ``core.config_policy.rigs``
/ ``tools.goal_tools`` build their views — or those views move to call-time
``profile()`` reads in the same commit. Callers that can filter to what is
installed use :func:`resolve`.
"""
from __future__ import annotations

import sys
from typing import Dict, Iterable, Optional, Tuple

# The supervised server-container default; `toolset:full` names the same set
# ("Full server stack (mirrors server_default_tools())").
_SERVER_DEFAULT = ("filesystem", "task", "web_fetch", "perplexity", "email", "mcp", "anysite")
_BASE = ("filesystem", "task")
_CLI_BASE = ("filesystem", "task", "web_fetch")
# 071 W0: `defi_data` is READ-only token sight (no signer, no broadcast). It
# registers only under DEFI_DATA_ENABLED, so listing it here loads nothing on an
# instance that has not enabled it — and it no longer rides the SPEND arm
# (grant:defi_autonomous) to reach a research or chat turn.
_CRYPTO_RESEARCH = ("filesystem", "task", "perplexity", "anysite", "web_fetch",
                    "polymarket_data", "hyperliquid_data", "defi_data")

PROFILES: Dict[str, Tuple[str, ...]] = {
    # ── per-surface defaults ────────────────────────────────────────────────
    "default:server": _SERVER_DEFAULT,
    # A bare SessionRequest (supervised); was a literal in task_agent_lite.py.
    "default:session": ("browser", "filesystem", "task"),
    # Static base of the CLI / "default" toolset; tool_defaults adds the
    # dynamic coding / anysite / defi_data members at call time.
    "default:cli": _CLI_BASE,

    # ── the POLYROB_AGENT_TOOLSET vocabulary (key order = the init wizard's) ─
    "toolset:minimal": _BASE,
    "toolset:safe": _BASE,
    # resolve_toolset("default") adds the dynamic members, so choosing "default"
    # by name == an unset POLYROB_AGENT_TOOLSET (`polyrob init` writes it).
    "toolset:default": _CLI_BASE,
    "toolset:research": _CRYPTO_RESEARCH,
    "toolset:trading_research": _CRYPTO_RESEARCH,
    "toolset:coding": ("filesystem", "task", "coding"),
    "toolset:development": ("filesystem", "task", "coding", "browser"),
    "toolset:browser": ("filesystem", "task", "browser"),
    # Public discovery plus the two account-authoritative X rails; writes keep
    # their per-action approval and TWITTER_ENABLED boundaries.
    "toolset:social": ("filesystem", "task", "anysite", "perplexity", "web_fetch",
                       "twitter", "x_browser"),
    "toolset:full": _SERVER_DEFAULT,
    # Flagship "earn real money, safely" goal: research / browse / code only.
    "toolset:earn": ("filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding"),
    # Owner interactive chat supervised default (surfaces/telegram/interactive_tools.py).
    "toolset:owner_interactive": ("goal", "twitter", "web_fetch", "filesystem", "task",
                                  "defi_data"),

    # ── the autonomous grant (013 §2.3) and its armed extensions ────────────
    # NEVER money-spend and NEVER host/compute (those ride AGENT_COMPUTE_POSTURE).
    "grant:autonomous": ("filesystem", "task", "web_fetch", "knowledge",
                         "twitter", "email", "anysite", "perplexity",
                         "browser", "mcp", "coding", "x402_invoice",
                         "goal", "cronjob",
                         "x_browser"),
    # Added only when DEFI_AGENT_AUTONOMY is armed (agents/task/constants.py).
    "grant:defi_autonomous": ("defi_data", "defi_trade"),
    "grant:defi_launch_autonomous": ("launchpad", "dapp_browser"),
    # 068 X4: x402 auto-pay rides the same armed key. Bounded by the x402 lane
    # (X402_AUTONOMOUS_MAX_USD act-and-report, owner queue above) and the
    # WALLET_VENUE_DAILY_CAP_X402_USD PolicyGate venue cap.
    "grant:x402_autonomous": ("x402_pay",),
    # ... and only when the optional agent-NFT package is installed.
    "grant:agent_nft": ("agent_nft",),
    # The meta ids an AMBIENT session never holds (agent-callable capabilities,
    # not a toolset): subtracted from grant:autonomous by the ambient defaults.
    "exclude:ambient": ("goal", "cronjob"),

    # ── the autonomous rigs (057 WS-A) ──────────────────────────────────────
    "rig:money_rail": ("defi_data", "defi_trade", "filesystem", "task", "message"),
    # 071: treasury-trading requires `reconcile` before any P&L post, so the
    # social rig carries the read tool (never the spend one).
    "rig:social": ("twitter", "x_browser", "filesystem", "task", "message", "web_fetch",
                   "defi_data"),
    "rig:research": ("web_fetch", "anysite", "knowledge", "filesystem", "task", "message",
                     "defi_data"),
    "rig:ops": ("filesystem", "task", "goal", "cronjob", "message"),

    # ── the goal rails ──────────────────────────────────────────────────────
    # Proposal 001/009/029: what an agent-created goal may request (a SET — order
    # carries no meaning here, kept for a stable spelling).
    "ceiling:self_goal": ("filesystem", "task", "browser", "perplexity", "mcp", "anysite",
                          "coding", "web_fetch", "twitter", "email", "message",
                          "x402_invoice", "knowledge", "defi_data"),
    # Proposal 009 #1: unioned into any self-created goal that carries tools.
    "baseline:self_goal": ("filesystem", "task", "web_fetch", "knowledge"),
    # What a self-decomposed child goal may inherit from its parent.
    "inherit:child_goal": ("filesystem", "task", "browser", "perplexity", "mcp",
                           "anysite", "coding", "defi_data"),
}


#: Members that are controller ACTION ids, not container tools (never classified).
ACTION_MEMBERS = frozenset({"message"})


def provided(tool_id: str) -> bool:
    """Whether this install provides *tool_id*: a classified tool or an action id.
    An unclassified name belongs to a pack that is not installed (or whose rows
    were refused in phase 1); a classified pack tool is withheld once phase 2
    left its pack disabled or refused (``core.packs.state.tool_withheld``)."""
    if tool_id in ACTION_MEMBERS:
        return True
    from core.tool_capabilities import is_classified
    if not is_classified(tool_id):
        return False
    # Read the loader state only when a loader ran in this process: importing it
    # here would pull core.packs into processes that must never load packs (the
    # signer, ratcheted by tests/unit/core/packs/test_pack_ratchets.py).
    state = sys.modules.get("core.packs.state")
    return state is None or not state.tool_withheld(tool_id)


def provided_only(ids: Iterable[str]) -> list:
    """*ids* filtered to :func:`provided`, order kept — for a call-time consumer
    of an import-time view (a pack's phase 2 runs after the views are built)."""
    return [t for t in ids if provided(t)]


def profile(name: str) -> Tuple[str, ...]:
    """The ordered ids of profile *name* that this install provides
    (:func:`provided`). ``KeyError`` on an unknown name — a typo in code must
    fail loudly (runtime names from env / payloads are validated by their
    owners, e.g. ``rigs.rig_tools``)."""
    return tuple(t for t in PROFILES[name] if provided(t))


def names(prefix: str) -> Tuple[str, ...]:
    """The unprefixed names under *prefix* (e.g. ``"toolset"``), in table order."""
    head = prefix.rstrip(":") + ":"
    return tuple(k[len(head):] for k in PROFILES if k.startswith(head))


def resolve(name: str, installed: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """Profile *name*, order kept, filtered to *installed* ids when given."""
    ids = profile(name)
    if installed is None:
        return ids
    have = set(installed)
    return tuple(t for t in ids if t in have)
