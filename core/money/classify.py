"""The ONE "is this money" predicate (067 P1b).

Two questions, both derived — nothing here is a list:

* :func:`money_tool` — is this TOOL classified ``money`` in
  ``core/tool_capabilities.py``?
* :func:`money_action` — does this ACTION belong to a money tool? The owning
  tool comes from the action's ``core/verb_policy.py`` row when the row names
  one, else from the action-name namespace (``{tool_id}_{action}``,
  ``tools/controller/tool_management.py``), LONGEST tool id first so
  ``polymarket_data_*`` (reads) stays apart from ``polymarket_*`` (money).
  The namespace fallback is what covers a name no row describes (an MCP
  server's tool, a future pack's verb).

Replaces the copies that used to live in ``room_policy._is_money_call``,
``approval_queue._is_money_action`` and ``metering_gate.MONEY_TOOLS`` — those
now call here (``tests/unit/core/money/test_money_kernel_ratchet.py``).

Tier-0: reads two core tables, imports nothing above ``core``.
"""
import logging
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)


def money_tool_ids() -> FrozenSet[str]:
    """Every tool id the capability table classifies ``money``."""
    from core.tool_capabilities import ids_with
    return frozenset(ids_with("money"))


def money_tool(tool_id) -> bool:
    """True when *tool_id* (an ``mcp:`` prefix is ignored) is a money tool."""
    name = str(tool_id)
    if name.startswith("mcp:"):
        name = name[4:]
    return name in money_tool_ids()


#: 085 A5: flat action names (``{server}_{tool}``) of an MCP server the owner
#: marked ``"money": true`` in mcp.json. The MCP registrar adds them when it
#: registers the server's actions; no capability-table row can name them.
_MONEY_MCP_ACTIONS: set = set()


def register_money_actions(names) -> None:
    """Classify each action in *names* as money (an owner-marked MCP server)."""
    _MONEY_MCP_ACTIONS.update(str(n) for n in (names or ()) if n)


def owning_tool(name: str) -> Optional[str]:
    """The tool id that owns action *name*, or None.

    The verb-policy row's ``tool`` when it names one; otherwise the longest
    capability-table id that *name* equals or is namespaced under.
    """
    from core.tool_capabilities import TOOL_CAPABILITIES
    from core.verb_policy import policy_for
    row = policy_for(name)
    if row is not None and row.tool:
        return row.tool
    matches = [t for t in TOOL_CAPABILITIES if name == t or name.startswith(t + "_")]
    return max(matches, key=len) if matches else None


def money_action(name) -> bool:
    """True when action *name* belongs to a money tool (or to an MCP server
    the owner marked money)."""
    if str(name) in _MONEY_MCP_ACTIONS:
        return True
    owner = owning_tool(name)
    return bool(owner) and money_tool(owner)


#: 112 W3: ``fn(payload) -> bool`` — does the payload pin a money-class skill.
_MONEY_SKILL_PROBE = None


def register_money_skill_probe(fn) -> None:
    """Registered by ``core.skills.catalog`` at import (the skill index lives above here)."""
    global _MONEY_SKILL_PROBE
    _MONEY_SKILL_PROBE = fn


def is_money_class(payload) -> bool:
    """True when an autonomous run (a cron job or goal *payload*) is MONEY-CLASS.

    The ONE answer for routing (``HEAVY_MODEL``), tick ordering, pre-emption and the
    wake-gate carve-out (2026-10-14 review B-1). Three signals, any one suffices:

    * ``payload.priority == "money"`` — the explicit label;
    * a pinned builtin or pack skill labelled ``polyrob-money-class`` (112; the probe
      ``core.skills.catalog`` registers through :func:`register_money_skill_probe`);
    * the run's TOOLSET holds a money tool — ``payload.tools``, else the rig
      (``payload.rig`` > ``AUTONOMOUS_RIG_DEFAULT``) resolved through
      ``core.config_policy.rigs.resolve_rig_tools``. The live money jobs carry
      ``rig=money_rail`` and no priority; keying on priority alone routed none of them.

    *payload* may be a dict or its stored JSON text. Fail-closed to False (a bad
    payload is not money-class; every spend gate still runs on its own), with a
    warning. A caller that must refuse on an unknown answer uses
    :func:`money_class_strict`.
    """
    try:
        return money_class_strict(payload)
    except Exception:
        logger.warning("money class unknown for a run payload; read as not money",
                       exc_info=True)
        return False


def money_class_strict(payload) -> bool:
    """:func:`is_money_class`, but a classification that fails RAISES."""
    if isinstance(payload, (str, bytes)):
        import json
        payload = json.loads(payload or "{}")
    if not isinstance(payload, dict):
        return False
    if str(payload.get("priority") or "").strip().lower() == "money":
        return True
    # 112 W3: work that DECIDES money — a pinned builtin/pack skill labelled money-class
    # (a probe ``core.skills.catalog`` registers; an import here closes a core cycle).
    if _MONEY_SKILL_PROBE is not None and _MONEY_SKILL_PROBE(payload):
        return True
    from core.config_policy.rigs import resolve_rig_tools
    ids = resolve_rig_tools(payload) or []
    return any(money_tool(t) for t in ids)
