"""The emit order of ``tools[]``: what most rigs hold comes first.

``tools[]`` opens the cached prefix, and a prefix cache stops at the first byte two
requests differ. A pure name sort put an optional tool (``agent_avatar``) first, so two
rigs differed at index 2 and shared nothing. Each row is a name prefix, in falling order
of how many rigs hold it (measured on ``tests/golden/prompt_surface/*.tools.json``, and
``tests/unit/tools/controller/registry/test_stable_schema_order.py`` checks the head).
Everything else follows by name. A row names no capability: a tool that is not on the
list only sorts later.
"""
from __future__ import annotations

from typing import Tuple

COMMON_TOOLS_FIRST: Tuple[str, ...] = (
	# every rig
	"done", "send_message", "load_skill", "read_skill_resource",
	"task_todo_", "tool_describe", "tool_search", "compute",
	# all but the room
	"filesystem_", "web_fetch_",
	# most rigs
	"agent_status", "insights", "load_tool", "message", "tool_call",
	"agent_avatar", "autonomy_control", "contact_history", "delegate_task", "offer_manage",
	"order_deliver", "owner_ask", "owner_doc_manage", "preferences", "propose_action",
	"room_moderate", "room_read", "self_context_manage", "skill_manage", "trade_mandate_propose",
)


def emit_rank(name: str) -> Tuple[int, str]:
	"""The sort key of one action name."""
	for rank, prefix in enumerate(COMMON_TOOLS_FIRST):
		if name.startswith(prefix):
			return rank, name
	return len(COMMON_TOOLS_FIRST), name
