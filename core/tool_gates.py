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
from dataclasses import dataclass
from typing import Callable, Dict, Optional

Gate = Callable[[], bool]

#: ``ToolGate.tier`` values (the <tool-availability> vocabulary).
GATE_TIERS = frozenset({"loadable", "disabled", "reserved"})


#: key -> the remedy text. ``{tool}``, ``{flag}`` and ``{set_flag}`` (the env line
#: the owner writes for ``{flag}``) are filled by :func:`remedy_text`.
REMEDIES: Dict[str, str] = {
    # -- a gate row's key (the <tool-availability> block and the catalog) --------
    "set_flag": "owner sets {set_flag}",
    "load_tool": 'call load_tool("{tool}") to use it',
    "compute_posture": ("owner raises AGENT_COMPUTE_POSTURE "
                        "(host axis; not part of AUTONOMY_MODE)"),
    "compute_flag": "owner sets {set_flag} — raising the posture changes nothing here",
    "compute_session": ("not in this session's toolset — goal/cron runs carry the compute "
                        "tools only at posture>=1 with the flag on; otherwise run it in an "
                        "interactive (owner) session or raise ONE ask"),
    "cron_posture": ("owner raises AUTONOMY_POSTURE or sets "
                     "POLYROB_OVERRIDES=CRON_ENABLED=true"),
    "mcp_servers": "owner sets {set_flag} and configures servers",
    "email_credentials": "owner configures email creds; sends follow outbound.policy",
    "x402_invoice": "owner sets {set_flag} (auto-ON in autonomous mode)",
    "x_credentials": ("owner configures X user credentials; "
                      "POLYROB_OVERRIDES=TWITTER_ENABLED=true unlocks writes"),
    "browser_install": ("ask the owner to install the browser service on this host "
                        "(the browser step of the self-hosting guide)"),
    "browser_service": ("ask the owner to check the browser service "
                        "(polyrob-browser.service) on this host"),
    "x_browser_session": ("owner enables X_BROWSER_ENABLED and runs "
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


__all__ = ["GATE_TIERS", "Gate", "REMEDIES", "ToolGate", "register_gate", "gate_for",
           "gate_on", "remedy_text"]
