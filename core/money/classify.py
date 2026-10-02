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
from typing import FrozenSet, Optional


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
    """True when action *name* belongs to a money tool."""
    owner = owning_tool(name)
    return bool(owner) and money_tool(owner)
