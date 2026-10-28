"""Tool enable gates — the ONE gate module (067 P1).

Two halves, one module:

1. **Static declaration** — :class:`ToolGate`, carried as the ``gate`` field of the
   per-tool rows in ``core/tool_capabilities.py``. It names the flag and the
   agent-facing disclosure (label, tier ``loadable|disabled|reserved``, remedy).
   ``tools.controller.tool_load_report.TOOL_GATE_FLAGS`` and
   ``agents.task.agent.core.tool_availability.GATED_TOOL_REGISTRY`` are VIEWS of it.
2. **Runtime predicate** — ``tool_id -> () -> bool``. ``core`` and ``agents`` may not
   import ``tools`` (``tests/test_layering_ratchet.py``), yet several readers there
   need a tool's live enable gate (is ``anysite`` on? is ``hf_deploy`` on?). The gate
   function stays next to its tool; the tool module registers it here at import
   (:func:`register_gate`), and the lower tiers look it up by tool id.
   ``core.tool_capabilities`` re-exports these functions; the store lives here.

3. **Remedy text** (081 B-1) — :data:`REMEDIES` is the ONE home of the text that
   tells the agent how a gated tool is unblocked. A gate row (here or in a
   ``pack.toml``) names a KEY, never prose, and every reader renders the text
   with :func:`remedy_text`: the ``<tool-availability>`` block
   (``agents/task/agent/core/tool_availability.py``) and the ``<tool-catalog>`` /
   ``load_tool`` / not-loaded refusals (``tools/tool_disclosure.py``). So one tool
   has one gate text on every surface.

``import tools`` loads every registrar (``tools/__init__.py``). A reader that runs
with the tool tier not loaded gets ``None`` from :func:`gate_for`; it must treat
that as "the tool is not available", never as enabled.
"""
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional

Gate = Callable[[], bool]

#: ``ToolGate.tier`` values (the <tool-availability> vocabulary).
GATE_TIERS = frozenset({"loadable", "disabled", "reserved"})


#: key -> the remedy text. ``{tool}``, ``{flag}`` and ``{set_flag}`` (the env line
#: the owner writes for ``{flag}``) are filled by :func:`remedy_text`.
REMEDIES: Dict[str, str] = {
    # -- a gate row's key (the <tool-availability> block and the catalog) --------
    "set_flag": "operator sets {set_flag}",
    "load_tool": 'call load_tool("{tool}") to use it',
    "compute_posture": ("operator raises AGENT_COMPUTE_POSTURE "
                        "(host axis; not part of AUTONOMY_MODE)"),
    "compute_flag": "operator sets {set_flag} — raising the posture changes nothing here",
    "compute_session": ("not in this session's toolset — goal/cron runs carry the compute "
                        "tools only at posture>=1 with the flag on; otherwise run it in an "
                        "interactive (owner) session or raise ONE ask"),
    "cron_posture": ("operator raises AUTONOMY_POSTURE or sets "
                     "POLYROB_OVERRIDES=CRON_ENABLED=true"),
    "mcp_servers": ("operator sets {set_flag}, then the owner adds a server with "
                    "/mcp add <id> <https-url>"),
    # 093 A1/B2: the flag is on but no server is configured — MCPTool is off.
    "no-mcp-server": ("MCP_ENABLED is on, but no MCP server is configured, so the tool "
                      "is off. The owner adds one with /mcp add <id> <https-url> (it "
                      "works from the next turn); the agent cannot add it itself, and a "
                      "restart alone changes nothing"),
    # 093 A3: the exec backend cannot start for this process (prod: docker socket).
    "backend-unavailable": ("the exec backend ({flag}) cannot start here, so every call "
                            "fails. The operator sets a working CODE_EXEC_BACKEND "
                            "(`polyrob doctor` run as the agent's user lists the working "
                            "ones); a retry or a restart alone "
                            "changes nothing"),
    "email_credentials": "operator configures email creds; sends follow outbound.policy",
    "x402_invoice": "operator sets {set_flag} (auto-ON in autonomous mode)",
    "x_credentials": ("operator configures X user credentials; "
                      "POLYROB_OVERRIDES=TWITTER_ENABLED=true unlocks writes"),
    "browser_install": ("ask the owner to install the browser service on this host "
                        "(the browser step of the self-hosting guide)"),
    "browser_service": ("ask the owner to check the browser service "
                        "(polyrob-browser.service) on this host"),
    "x_browser_session": ("operator enables X_BROWSER_ENABLED and runs "
                          "`polyrob x-account capture-session`"),
    # ⚠️ Do NOT reintroduce "request it on the goal payload" in "money". A goal the
    # agent creates itself can never carry a money tool: goal_create filters the
    # whole money-SPEND set out (tools/goal_tools.py), deliberately, so an injected
    # goal cannot launder itself into a trade. Naming that as the remedy sent the
    # prod agent round a loop ~50 times in two weeks. ⚠️ ...unless the money
    # regime is ARMED ("money_armed"): then the self-goal ceiling holds the tool
    # and goal_create keeps it (prod 2026-10-12: four trading goals written without
    # defi_trade, every one blocked). tools/tool_disclosure.py picks the key with
    # the one predicate, ``allowed_self_goal_tools``.
    "money": (
        "money tools are explicit-grant-only: load_tool never loads one. A goal you "
        "create yourself can NEVER carry one — goal_create strips money tools by "
        "design, so do not retry by adding it to your own goal payload. Only the "
        "owner/operator grants it: on a goal THEY seeded, or at session creation. If "
        "you have a candidate and no grant, record it where the granted run will read "
        "it and say so plainly — then escalate ONCE, not every run."),
    "money_armed": (
        "money tools never load mid-run (load_tool never loads one). On this instance "
        "the money regime is ARMED, so a goal you create CAN carry it: "
        "goal_create(..., rig='money_rail') or tools=[..., '{tool}']. This run lacks "
        "it because its goal did not list it — create the goal with it (caps, "
        "simulation and the pause still bound every spend)."),
    # Appended by the <tool-catalog> / load_tool / not-loaded refusal to a money
    # gate, never by the <tool-availability> block: that block is in the system
    # prompt of a room and a correspondent session too, which get no owner moves.
    "money_owner_move": ("When the OWNER asks for a money move, call propose_action: "
                         "the move runs from their seat."),
    # -- the catalog's own statuses (tools/tool_disclosure.py) -------------------
    "leaf-blocked": ("not available to delegated sub-agents — report back and let "
                     "the parent agent run this itself"),
    "autonomous-ceiling": ("not in this autonomous run's self-service tool ceiling; "
                           "the owner must grant it explicitly"),
    "needs-config": "needs config: {flag} — message the owner / file an ops ask",
    "not-constructed": ("not constructed in this deploy shape — message the owner / "
                        "file an ops ask"),
    "unknown-tool": "not a known tool id — see <tool-catalog> for the valid ids",
}


#: 093 C1 — WHO can act on a gate. ``agent`` (the agent itself), ``owner-verb``
#: (the owner types or taps a seat verb), ``operator-env`` (the person who runs
#: the host: the env file, a backend, a unit), ``deploy`` (a code/deploy change).
#: ⚠️ The actor word of a :data:`REMEDIES` text agrees with its row: a text that
#: names an env-only fix says "operator", never "owner" (a seat's ``/config set``
#: refuses every flag those texts name). Ratcheted.
ACTORS = ("agent", "owner-verb", "operator-env", "deploy")
#: 093 C1 — WHEN a fix applies once it is made.
APPLIES = ("now", "next-turn", "new-session", "restart", "deploy")


@dataclass(frozen=True)
class RemedyActor:
    """Who fixes a gate, when the fix applies, and the verb or line they use."""
    who: str
    when: str
    verb: str = ""

    def __post_init__(self):
        if self.who not in ACTORS:
            raise ValueError(f"who={self.who!r} not in {ACTORS}")
        if self.when not in APPLIES:
            raise ValueError(f"when={self.when!r} not in {APPLIES}")


#: 093 C1 — the ONE who/when table, next to the remedy text it explains. Keyed
#: by every :data:`REMEDIES` key AND every gate reason
#: ``tools.tool_disclosure.resolve_tool_status`` can return (a reason with no
#: REMEDIES key of its own: its remedy text comes from the gate). Ratcheted by
#: ``tests/unit/core/test_093_remedy_actors.py``. The operations-manual skill
#: (``data/prompts/skills/harness-manual``) is generated from this table.
REMEDY_ACTORS: Dict[str, RemedyActor] = {
    # -- REMEDIES keys --------------------------------------------------------
    "set_flag": RemedyActor("operator-env", "restart", "POLYROB_OVERRIDES=<FLAG>=true"),
    "load_tool": RemedyActor("agent", "now", 'load_tool("<id>")'),
    "compute_posture": RemedyActor("operator-env", "restart", "AGENT_COMPUTE_POSTURE=<n>"),
    "compute_flag": RemedyActor("operator-env", "restart", "POLYROB_OVERRIDES=<FLAG>=true"),
    "compute_session": RemedyActor("owner-verb", "new-session",
                                   "run it from an owner chat, or raise ONE owner_ask"),
    "cron_posture": RemedyActor("operator-env", "restart", "POLYROB_OVERRIDES=CRON_ENABLED=true"),
    # MCP_ENABLED is a posture member: a direct MCP_ENABLED= line is ignored.
    "mcp_servers": RemedyActor("operator-env", "restart", "POLYROB_OVERRIDES=MCP_ENABLED=true"),
    "no-mcp-server": RemedyActor("owner-verb", "next-turn", "/mcp add <id> <https-url>"),
    "backend-unavailable": RemedyActor("operator-env", "restart", "CODE_EXEC_BACKEND=<backend>"),
    "email_credentials": RemedyActor("operator-env", "restart", "the email credentials"),
    "x402_invoice": RemedyActor("operator-env", "restart", "POLYROB_OVERRIDES=<FLAG>=true"),
    "x_credentials": RemedyActor("operator-env", "restart", "the X user credentials"),
    "browser_install": RemedyActor("operator-env", "restart", "install the browser service"),
    "browser_service": RemedyActor("operator-env", "restart", "polyrob-browser.service"),
    "x_browser_session": RemedyActor("operator-env", "new-session",
                                     "polyrob x-account capture-session"),
    "money": RemedyActor("owner-verb", "new-session", "/goal grant <id> tools=<tool>"),
    "money_armed": RemedyActor("agent", "new-session", "goal_create(..., rig='money_rail')"),
    "money_owner_move": RemedyActor("owner-verb", "now", "propose_action"),
    "leaf-blocked": RemedyActor("agent", "now", "report to the parent agent"),
    "autonomous-ceiling": RemedyActor("owner-verb", "new-session",
                                      "/goal grant <id> tools=<tool>"),
    "needs-config": RemedyActor("operator-env", "restart", "the named config"),
    "not-constructed": RemedyActor("deploy", "deploy", "a deploy shape that builds it"),
    "unknown-tool": RemedyActor("agent", "now", "a valid id from <tool-catalog>"),
    # -- gate reasons with no REMEDIES key of their own -----------------------
    "unavailable-on-this-deploy": RemedyActor("operator-env", "restart", "the named config"),
    "custody-no-browser": RemedyActor("operator-env", "restart", "the remote browser rail"),
    "credentials-rejected": RemedyActor("operator-env", "restart", "the rejected credentials"),
}


#: 093 C1 — every ``gated:<reason>`` the ONE resolver
#: (``tools.tool_disclosure.resolve_tool_status``) can return, including a
#: constructed tool's own ``availability_gap`` reason. Ratcheted against the
#: resolver's literals; every one has a :data:`REMEDY_ACTORS` row.
STATUS_REASONS = ("money", "leaf-blocked", "autonomous-ceiling", "unavailable-on-this-deploy",
                  "custody-no-browser", "credentials-rejected", "unknown-tool",
                  "no-mcp-server", "backend-unavailable")


def _template_pattern(template: str) -> "re.Pattern":
    parts = re.split(r"\{(?:tool|flag|set_flag)\}", template)
    return re.compile(".*?".join(re.escape(p) for p in parts), re.DOTALL)


def remedy_key_for_text(text: str) -> Optional[str]:
    """The :data:`REMEDIES` key whose rendered text appears in *text*, else None.

    A gate status carries the rendered remedy, not its key (the catalog line
    shows prose); this reads the key back from the ONE text home, so no second
    status field exists. The longest template wins (``money`` vs a shorter
    prefix)."""
    text = str(text or "")
    if not text:
        return None
    for key in sorted(REMEDIES, key=lambda k: -len(REMEDIES[k])):
        if _template_pattern(REMEDIES[key]).search(text):
            return key
    return None


def remedy_actor(reason: str = "", remedy: str = "") -> Optional[RemedyActor]:
    """Who/when for a gate: by the remedy text's key first (the most specific
    fact), then by the gate reason. None for an unknown reason."""
    key = remedy_key_for_text(remedy)
    if key is not None and key in REMEDY_ACTORS:
        return REMEDY_ACTORS[key]
    return REMEDY_ACTORS.get(str(reason or ""))


def remedy_text(key: str, *, tool: str = "", flag: str = "",
                set_flag: Optional[str] = None) -> str:
    """The remedy text for *key*. ``KeyError`` on an unknown key: a gate row is
    validated at construction, so a miss here is a code bug, not a config.

    ``set_flag`` is the env line for ``flag``; the default is the posture-member
    line (every flag a gate row names is a member). A caller with a non-member
    flag passes ``core.config_policy.members.set_line(flag)`` (this module may
    not import it: the lazy edge closes an import cycle)."""
    if set_flag is None:
        set_flag = f"POLYROB_OVERRIDES={flag}=true" if flag else ""
    return REMEDIES[key].format(tool=tool, flag=flag, set_flag=set_flag)


@dataclass(frozen=True)
class ToolGate:
    """A tool's static gate declaration (the ``gate`` field of its row).

    - ``flag`` — the env flag whose OFF value explains "this tool is not
      registered" (the ``TOOL_GATE_FLAGS`` view; checked against the flags catalog).
    - ``label``/``tier``/``remedy`` — the line disclosed to the agent in the
      <tool-availability> block (the ``GATED_TOOL_REGISTRY`` view); a gate with no
      ``tier`` is not disclosed. ``remedy`` is a KEY of :data:`REMEDIES`, never
      prose (081 B-1); the text is rendered from there.
    """

    flag: Optional[str] = None
    label: Optional[str] = None
    tier: Optional[str] = None
    remedy: Optional[str] = None

    def __post_init__(self):
        if self.tier is not None and self.tier not in GATE_TIERS:
            raise ValueError(f"tier={self.tier!r} not in {sorted(GATE_TIERS)}")
        if (self.tier is None) != (self.label is None) or (self.tier is None) != (self.remedy is None):
            raise ValueError("label, tier and remedy are declared together")
        if self.remedy is not None and self.remedy not in REMEDIES:
            raise ValueError(f"remedy={self.remedy!r} is not a key of core.tool_gates.REMEDIES "
                             f"(a gate row names a key, never prose)")

    def disclosure(self, tool_id: str = "") -> Optional[tuple]:
        """``(label, tier, remedy text)`` or ``None`` when the gate is not disclosed."""
        if self.tier is None:
            return None
        return (self.label, self.tier,
                remedy_text(self.remedy, tool=tool_id, flag=self.flag or ""))

_GATES: Dict[str, Gate] = {}


def register_gate(tool_id: str, fn: Gate) -> None:
    """Called from the tool tier at import. Last registration wins."""
    _GATES[tool_id] = fn


def gate_for(tool_id: str) -> Optional[Gate]:
    """The registered gate for ``tool_id``, or ``None`` when none is registered."""
    return _GATES.get(tool_id)


def gate_on(tool_id: str) -> bool:
    """``True`` only when a gate is registered AND it returns true.

    Fail-closed: no gate or a gate that raises is ``False``.
    """
    fn = _GATES.get(tool_id)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


__all__ = ["ACTORS", "APPLIES", "GATE_TIERS", "Gate", "REMEDIES", "REMEDY_ACTORS", "RemedyActor",
           "STATUS_REASONS",
           "ToolGate", "register_gate", "gate_for", "gate_on", "remedy_actor",
           "remedy_key_for_text", "remedy_text"]
