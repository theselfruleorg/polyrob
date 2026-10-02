"""060 WS-2 — the ONE foundation order, as a table (2026-09-23).

Before this module the foundation order existed twice, hand-maintained, in
``retrieval.get_messages`` and ``retrieval.get_messages_for_llm`` (each copy
commented as matching the other), and a third time in
``foundation_replay.FOUNDATION_SLOTS``. Nothing tested that they agreed.

``FOUNDATION_LAYERS`` is now the SSOT: both assemblies and the replay slots
derive from it, and ``tests/test_foundation_order_ratchet.py`` asserts one order,
that every ``MessageOrigin`` maps to exactly one tier, and that no VOLATILE
block sits in the cached prefix.

The four tiers (060 §4.1):

- ``FROZEN``   — system prompt, SOUL, runtime facts. Stable per session; never
  agent-writable.
- ``STANDING`` — owner rules and doctrine (skills). Stable per session; written
  through quarantine → owner promote.
- ``SCOPED``   — the rail's own instruction for this run (the task, a cron or
  goal prose, a runtime directive for this turn).
- ``VOLATILE`` — H-MEM, recall, tool results, correspondent data. Never cached,
  and EVIDENCE, never instruction.

The precedence rule (060 §4.2, owner decision Q1 2026-09-23): a runtime GATE
always wins. Within the gates: FROZEN > the latest genuine owner rule
(STANDING) > the rail's SCOPED instruction > doctrine (STANDING) > VOLATILE
evidence, which is never an instruction at all.

⚠️ 063 prompt cache: the ORDER of this table is the wire order of the cached
prefix. Reordering it rewrites every live session's prefix once and must update
``tests/unit/agents/task/agent/messages/test_prefix_stability.py`` deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from modules.llm.messages import MessageOrigin

# The four tiers are core-tier data (core/instruction_surfaces.py) so the status
# snapshot and the sweep verb name them without importing up.
from core.instruction_surfaces import FROZEN, SCOPED, STANDING, TIERS, VOLATILE  # noqa: E402


@dataclass(frozen=True)
class FoundationLayer:
	"""One pinned foundation block.

	``key``/``attr``/``tokens_attr`` are the replay JSON key, the MessageManager
	attribute and its token attribute. ``origin`` is the origin a cold rebuild
	gives the message (None for the system prompt and the task). An attribute
	may hold one message or a tuple of messages (a layer that renders several
	declared blocks — 060 WS-1).
	"""

	key: str
	attr: str
	tokens_attr: str
	origin: Optional[str]
	tier: str
	cached: bool = True
	#: layers that share a group are ALTERNATIVE renders of one block (060 WS-1:
	#: the combined self-context message vs its declared blocks). Replay treats a
	#: group as one unit so a persisted render never stacks on a live one.
	group: str = ""
	#: a layer added after F13 shipped: a persisted blob without its key is
	#: still whole (the key reads as "unset when saved").
	optional: bool = False


#: THE foundation order, in wire order. Everything else derives from it.
FOUNDATION_LAYERS: Tuple[FoundationLayer, ...] = (
	FoundationLayer("system_prompt", "_system_message", "_system_message_tokens",
	                None, FROZEN),
	FoundationLayer("runtime_identity", "_runtime_identity_message",
	                "_runtime_identity_tokens", MessageOrigin.RUNTIME_IDENTITY, FROZEN),
	FoundationLayer("environment", "_environment_message", "_environment_tokens",
	                MessageOrigin.ENVIRONMENT, FROZEN),
	FoundationLayer("self_context", "_self_context_message", "_self_context_tokens",
	                MessageOrigin.SELF_CONTEXT, STANDING, group="self_context"),
	# 060 WS-1: the same tier, unwelded — one message per declared block
	# (SELF_CONTEXT_BLOCKS below). Exactly one of the two self-context layers is
	# set in a session; SELF_CONTEXT_COMBINED=true selects the joined one.
	FoundationLayer("self_context_blocks", "_self_context_blocks",
	                "_self_context_blocks_tokens", MessageOrigin.SELF_CONTEXT, STANDING,
	                group="self_context", optional=True),
	FoundationLayer("project_context", "_project_context_message",
	                "_project_context_tokens", MessageOrigin.PROJECT_CONTEXT, STANDING),
	FoundationLayer("initial_task", "_initial_task_message", "_initial_task_tokens",
	                None, SCOPED),
	FoundationLayer("skill_catalog", "_skill_message", "_skill_message_tokens",
	                MessageOrigin.SKILL, STANDING),
	# 041 phase 2: the approved named workers (WORKERS_ENABLED). Frozen at
	# session start and unset when the flag is OFF or no worker is approved, so
	# the prefix is byte-identical then. STANDING: owner-approved offers, like
	# the skill catalog beside it.
	FoundationLayer("worker_catalog", "_worker_catalog_message", "_worker_catalog_tokens",
	                MessageOrigin.WORKER_CATALOG, STANDING, optional=True),
	FoundationLayer("tool_catalog", "_tool_catalog_message", "_tool_catalog_tokens",
	                MessageOrigin.TOOL_CATALOG, FROZEN),
)

#: H-MEM — the one VOLATILE block the assembly places itself. Not a replay slot:
#: it is rebuilt every step from the TaskContextManager.
HMEM_LAYER = FoundationLayer("hmem", "", "", MessageOrigin.MEMORY, VOLATILE, cached=False)


#: Every MessageOrigin → exactly one tier. A new origin with no row fails the
#: foundation-order ratchet, so a new kind of injected content must say what it
#: IS before it ships.
ORIGIN_TIERS = {
	MessageOrigin.RUNTIME_IDENTITY: FROZEN,
	MessageOrigin.ENVIRONMENT: FROZEN,
	MessageOrigin.TOOL_CATALOG: FROZEN,
	MessageOrigin.SELF_CONTEXT: STANDING,
	MessageOrigin.PROJECT_CONTEXT: STANDING,
	MessageOrigin.SKILL: STANDING,
	MessageOrigin.WORKER_CATALOG: STANDING,
	MessageOrigin.USER: SCOPED,
	MessageOrigin.GUIDANCE: SCOPED,
	MessageOrigin.INTERVENTION: SCOPED,
	MessageOrigin.APPROVAL: SCOPED,
	MessageOrigin.SYSTEM_NOTE: SCOPED,
	MessageOrigin.TOOL_NOTICE: SCOPED,
	MessageOrigin.TOOL_ADDITION: SCOPED,
	MessageOrigin.SELF_WAKE: SCOPED,
	MessageOrigin.MEMORY: VOLATILE,
	MessageOrigin.RECALL: VOLATILE,
	MessageOrigin.CORRESPONDENT: VOLATILE,
	MessageOrigin.COMPACTION_SUMMARY: VOLATILE,
	MessageOrigin.EPISODIC_DIGEST: VOLATILE,
	MessageOrigin.SESSION_BRIDGE: VOLATILE,
	MessageOrigin.GROUP_CONTEXT: VOLATILE,
	MessageOrigin.OWNER_THREAD: VOLATILE,
}


#: 060 WS-1 — the declared blocks of the SELF_CONTEXT tier, in wire order, each
#: with the tier that governs it. SELF_CONTEXT is the TIER name (an origin other
#: code reads); these are the BLOCK names.
SELF_CONTEXT_BLOCKS: Tuple[Tuple[str, str], ...] = (
	("awareness", FROZEN),     # "You act on behalf of OWNER X" + the correspondent frame
	("soul", FROZEN),          # identity.md + operating.md (operator-only)
	("owner_rules", STANDING), # owner.md — the owner's rules and facts
	("contract", STANDING),    # contract.md + the style line from typed prefs
	("self_doc", STANDING),    # the agent's evolving SELF doc (quarantined writes)
)


def self_context_combined() -> bool:
	"""``SELF_CONTEXT_COMBINED`` — re-join the self-context blocks into ONE
	message, byte-identical to the pre-060 weld. Default OFF (unwelded)."""
	from core.env import bool_env
	return bool_env("SELF_CONTEXT_COMBINED", False)


def tier_of(origin: Optional[str]) -> Optional[str]:
	"""The tier of an origin, or None for an unknown origin."""
	return ORIGIN_TIERS.get(origin) if origin else None


def hmem_placement() -> str:
	"""The first honest tier decision: where the VOLATILE H-MEM block rides.

	``"tail"`` (default) — a suffix after the conversation, outside the cached
	prefix. ``"prefix"`` — the legacy foundation placement
	(``HMEM_TAIL_PLACEMENT=false``), which puts a VOLATILE block inside the
	cached prefix and re-bills everything after it every step.
	"""
	from agents.task.constants import hmem_tail_placement
	return "tail" if hmem_tail_placement() else "prefix"


def layer_messages(manager: Any, layer: FoundationLayer) -> List[Any]:
	"""The message(s) one layer contributes right now (empty when unset)."""
	value = getattr(manager, layer.attr, None)
	if value is None:
		return []
	if isinstance(value, (list, tuple)):
		return [m for m in value if m is not None]
	return [value]


def foundation_messages(manager: Any) -> List[Any]:
	"""The pinned foundation, in table order.

	The system prompt is ALWAYS the first row — even when unset — because the
	assembly's fail-fast check reads ``messages[0]``; every other layer is
	skipped when empty.
	"""
	out: List[Any] = [getattr(manager, FOUNDATION_LAYERS[0].attr, None)]
	for layer in FOUNDATION_LAYERS[1:]:
		out.extend(layer_messages(manager, layer))
	return out


__all__ = [
	"FROZEN", "STANDING", "SCOPED", "VOLATILE", "TIERS",
	"FoundationLayer", "FOUNDATION_LAYERS", "HMEM_LAYER", "ORIGIN_TIERS",
	"SELF_CONTEXT_BLOCKS", "self_context_combined",
	"tier_of", "hmem_placement", "layer_messages", "foundation_messages",
]
