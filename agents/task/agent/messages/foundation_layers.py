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
	#: 2026-10-13 review A2: on a reload (restart / eviction) a layer whose FRESH
	#: render differs from the persisted bytes keeps the fresh render — the owner's
	#: rules, a deployed prompt fix, a newly approved skill and the live
	#: environment must reach a resumed chat. A cache miss once beats a stale
	#: rule. Byte-identical layers still replay (the cache survives).
	refresh_on_reload: bool = False
	#: on a reload a layer whose fresh render differs KEEPS its persisted bytes, and
	#: the changed lines ride the tail as a ``<state-delta>`` part, so a restart of a
	#: resumed chat stays warm and the change still reaches it.
	delta_on_reload: bool = False


#: THE foundation order, in wire order. Everything else derives from it.
#: A row the agent or the owner can write comes after every row they cannot, and a
#: row per session comes last, so one write colds only what follows it.
FOUNDATION_LAYERS: Tuple[FoundationLayer, ...] = (
	FoundationLayer("system_prompt", "_system_message", "_system_message_tokens",
	                None, FROZEN, refresh_on_reload=True),
	FoundationLayer("environment", "_environment_message", "_environment_tokens",
	                MessageOrigin.ENVIRONMENT, FROZEN, delta_on_reload=True),
	FoundationLayer("tool_catalog", "_tool_catalog_message", "_tool_catalog_tokens",
	                MessageOrigin.TOOL_CATALOG, FROZEN),
	FoundationLayer("self_context", "_self_context_message", "_self_context_tokens",
	                MessageOrigin.SELF_CONTEXT, STANDING, group="self_context",
	                delta_on_reload=True),
	# 060 WS-1: the same tier, unwelded — one message per declared block
	# (SELF_CONTEXT_BLOCKS below). The operator-only blocks sit here, ahead of the
	# skills; the blocks the owner and the agent write sit after them
	# (``self_context_blocks``). set_self_context_blocks fills both, and the
	# ``self_context`` row above is the welded alternative to the two of them.
	FoundationLayer("self_context_soul", "_self_context_soul",
	                "_self_context_soul_tokens", MessageOrigin.SELF_CONTEXT, STANDING,
	                group="self_context", optional=True, delta_on_reload=True),
	FoundationLayer("skill_catalog", "_skill_message", "_skill_message_tokens",
	                MessageOrigin.SKILL, STANDING, delta_on_reload=True),
	# 041 phase 2: the approved named workers (WORKERS_ENABLED). Frozen at
	# session start and unset when the flag is OFF or no worker is approved, so
	# the prefix is byte-identical then. STANDING: owner-approved offers, like
	# the skill catalog beside it.
	FoundationLayer("worker_catalog", "_worker_catalog_message", "_worker_catalog_tokens",
	                MessageOrigin.WORKER_CATALOG, STANDING, optional=True),
	FoundationLayer("self_context_blocks", "_self_context_blocks",
	                "_self_context_blocks_tokens", MessageOrigin.SELF_CONTEXT, STANDING,
	                group="self_context", optional=True, delta_on_reload=True),
	FoundationLayer("project_context", "_project_context_message",
	                "_project_context_tokens", MessageOrigin.PROJECT_CONTEXT, STANDING),
	# Model and provider only: a live /model swap rewrites one short row, and only
	# the two per-session rows follow it. The running version rides the tail.
	FoundationLayer("runtime_identity", "_runtime_identity_message",
	                "_runtime_identity_tokens", MessageOrigin.RUNTIME_IDENTITY, FROZEN,
	                refresh_on_reload=True),
	# 081 WS-3.3: everything above is the same for two runs of one job (or two
	# chats on one install); the two rows below differ per session — the
	# workspace path and the task (a cron task carries per-run text). They
	# come LAST, so a byte-prefix cache serves the catalogs to every run.
	FoundationLayer("workspace", "_workspace_message", "_workspace_tokens",
	                MessageOrigin.ENVIRONMENT, FROZEN, optional=True,
	                delta_on_reload=True),
	FoundationLayer("initial_task", "_initial_task_message", "_initial_task_tokens",
	                None, SCOPED),
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
	MessageOrigin.ACTION_LEDGER: VOLATILE,
	MessageOrigin.EPISODIC_DIGEST: VOLATILE,
	MessageOrigin.SESSION_BRIDGE: VOLATILE,
	MessageOrigin.GROUP_CONTEXT: VOLATILE,
	MessageOrigin.OWNER_THREAD: VOLATILE,
}


#: 060 WS-1 — the declared blocks of the SELF_CONTEXT tier, in wire order, each
#: with the tier that governs it. SELF_CONTEXT is the TIER name (an origin other
#: code reads); these are the BLOCK names. The FROZEN blocks ride the
#: ``self_context_soul`` row, the others the ``self_context_blocks`` row. The
#: agent's own doc is last, so an agent write colds no block the owner wrote.
SELF_CONTEXT_BLOCKS: Tuple[Tuple[str, str], ...] = (
	("awareness", FROZEN),     # "You act on behalf of OWNER X" + the correspondent frame
	("soul", FROZEN),          # identity.md + operating.md (operator-only)
	("contract", STANDING),    # contract.md + the style line from typed prefs
	("owner_rules", STANDING), # owner.md — the owner's rules and facts
	("self_doc", STANDING),    # the agent's evolving SELF doc (quarantined writes)
)


def tier_of(origin: Optional[str]) -> Optional[str]:
	"""The tier of an origin, or None for an unknown origin."""
	return ORIGIN_TIERS.get(origin) if origin else None


def owner_documents_visible(manager: Any) -> bool:
	"""Owner documents never cross a live correspondent/public-session boundary."""
	try:
		guard = getattr(manager, "_owner_context_allowed", None)
		return guard is None or bool(guard())
	except Exception:
		return False


def layer_visible(manager: Any, layer: FoundationLayer) -> bool:
	if layer.group != "self_context" and layer.key != "project_context":
		return True
	return owner_documents_visible(manager)


def layer_messages(manager: Any, layer: FoundationLayer) -> List[Any]:
	"""The message(s) one layer contributes right now (empty when unset)."""
	if not layer_visible(manager, layer):
		return []
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
	"SELF_CONTEXT_BLOCKS",
	"tier_of", "layer_messages", "foundation_messages", "owner_documents_visible",
]
