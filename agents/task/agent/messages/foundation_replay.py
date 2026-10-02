"""F13 — restart continuity for the cached prefix (2026-09-23).

A prompt cache is a PREFIX cache. ``message_history.json`` already restores the
conversation byte-for-byte, but every FOUNDATION block was re-rendered from the
live environment on the way back up: the system prompt, the runtime-identity
line, ``<environment>`` (which embeds the live tool set and the live budget),
self-context, project context, the skill catalog, the tool catalog, and the
emitted tool ORDER. A flag flip, a tool that failed to construct, an MCP server
that did not come back, or a plain deploy therefore re-wrote the whole prefix —
the one cost a long-lived gateway session pays most often.

This module persists the rendered foundation with the history and replays it
when the three things that MUST invalidate it are unchanged (a reference agent's three
rebuild triggers): the model, the serving provider and the working directory.
When any of them differs it logs ONE warning naming the cache cost and keeps the
fresh render.

Fail direction: a missing, partial or unreadable ``foundation`` object ALWAYS
falls back to the freshly built foundation. It never yields an empty prompt —
the blob is validated in full before a single slot is replaced.

Flag: ``FOUNDATION_REPLAY`` (default ON). ``false`` restores the pre-F13
rebuild-always behaviour exactly.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from core.env import bool_env
from agents.task.agent.messages.foundation_layers import FOUNDATION_LAYERS
from modules.llm.messages import HumanMessage, MessageOrigin, SystemMessage

logger = logging.getLogger(__name__)

#: the foundation slots, in wire order — DERIVED from the 060 WS-2 table
#: (``foundation_layers.FOUNDATION_LAYERS``), never a second hand-kept copy:
#: (json key, message attribute, token attribute, origin for a cold rebuild).
FOUNDATION_SLOTS = tuple(
	(layer.key, layer.attr, layer.tokens_attr, layer.origin)
	for layer in FOUNDATION_LAYERS
)

#: slots a pre-060 blob may lack and still be whole.
_OPTIONAL_KEYS = frozenset(layer.key for layer in FOUNDATION_LAYERS if layer.optional)

#: the identity triple that decides replay vs rebuild.
IDENTITY_KEYS = ("model", "provider", "cwd")


def foundation_replay_enabled() -> bool:
	"""``FOUNDATION_REPLAY`` — replay the persisted foundation. Default ON."""
	return bool_env("FOUNDATION_REPLAY", True)


def emitted_tool_names(tools: Optional[Sequence[Any]]) -> List[str]:
	"""The tool names in the order the provider schema list emits them.

	Handles both emitted shapes: OpenAI's ``{"type": "function", "function":
	{"name": …}}`` and Anthropic's flat ``{"name": …}``. Unknown entries are
	skipped — an order with holes is still a usable preference.
	"""
	names: List[str] = []
	for entry in tools or []:
		if not isinstance(entry, dict):
			continue
		name = entry.get("name")
		if not name:
			fn = entry.get("function")
			name = fn.get("name") if isinstance(fn, dict) else None
		if isinstance(name, str) and name:
			names.append(name)
	return names


def _content_of(message: Any) -> Optional[str]:
	"""The rendered string of a foundation message, or None when unset."""
	if message is None:
		return None
	content = getattr(message, "content", None)
	return content if isinstance(content, str) else None


def capture_foundation(manager: Any, tool_names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
	"""The ``foundation`` object to write into ``message_history.json``.

	Stores the EXACT rendered strings (already carrying their control-message
	envelope) plus the identity triple and the emitted tool order, so a reload
	can re-issue byte-identical request bytes.
	"""
	blob: Dict[str, Any] = {
		"model": str(getattr(manager, "model_name", "") or ""),
		"provider": str(getattr(manager, "provider_name", "") or ""),
		"cwd": os.getcwd(),
	}
	for key, attr, _tokens_attr, _origin in FOUNDATION_SLOTS:
		value = getattr(manager, attr, None)
		if isinstance(value, (list, tuple)):
			# 060 WS-1: a multi-message layer persists as a list of strings.
			blob[key] = [c for c in (_content_of(m) for m in value) if c is not None] or None
		else:
			blob[key] = _content_of(value)
	names = tool_names if tool_names is not None else getattr(manager, "_foundation_tool_names", None)
	blob["tool_names"] = [str(n) for n in (names or [])]
	return blob


def live_identity(manager: Any) -> Dict[str, str]:
	"""The identity triple as it stands right now."""
	return {
		"model": str(getattr(manager, "model_name", "") or ""),
		"provider": str(getattr(manager, "provider_name", "") or ""),
		"cwd": os.getcwd(),
	}


def _validated(blob: Any) -> Optional[Dict[str, Any]]:
	"""The blob if it is WHOLE, else None (→ keep the fresh render).

	Whole means: a dict, string identity keys, every present slot a string or
	null, a non-empty system prompt, and a list of string tool names. A
	half-written file must never be able to blank the prompt.
	"""
	if not isinstance(blob, dict):
		return None
	for key in IDENTITY_KEYS:
		if not isinstance(blob.get(key), str):
			return None
	for key, _attr, _tokens_attr, _origin in FOUNDATION_SLOTS:
		if key not in blob:
			if key in _OPTIONAL_KEYS:
				continue  # a pre-060 blob: the slot reads as unset when saved
			return None
		value = blob.get(key)
		if value is None or isinstance(value, str):
			continue
		if isinstance(value, list) and all(isinstance(v, str) for v in value):
			continue
		return None
	if not (blob.get("system_prompt") or "").strip():
		return None
	names = blob.get("tool_names")
	if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
		return None
	return blob


def _rebuild_message(live: Any, content: str, origin: Optional[str], is_system: bool) -> Any:
	"""One replayed foundation message carrying the persisted bytes."""
	if live is not None:
		message = type(live)(content=content)
		try:
			message.origin = getattr(live, "origin", origin) or origin or MessageOrigin.USER
		except Exception:  # pragma: no cover - defensive
			pass
		return message
	if is_system:
		return SystemMessage(content=content)
	return HumanMessage(content=content, origin=origin or MessageOrigin.USER)


def replay_foundation(manager: Any, blob: Any, log: Optional[Any] = None) -> bool:
	"""Replace the freshly built foundation with the persisted one.

	Returns True when the persisted bytes were installed, False when the fresh
	render stands (flag off, no/corrupt blob, or a changed identity triple —
	the last one logs ONE warning naming the cache cost).
	"""
	log = log or getattr(manager, "logger", None) or logger
	if blob is None:
		return False
	if not foundation_replay_enabled():
		return False

	valid = _validated(blob)
	if valid is None:
		log.warning(
			"foundation replay skipped (persisted foundation is incomplete or "
			"unreadable): prefix cache will miss once")
		return False

	live = live_identity(manager)
	if any(valid.get(key) != live.get(key) for key in IDENTITY_KEYS):
		log.warning(
			"foundation rebuilt (model/provider/cwd changed): prefix cache will miss once")
		return False

	# Build EVERY replacement before touching the manager, so a failure halfway
	# cannot leave a half-replayed foundation behind.
	planned = []
	# 060 WS-1: layers in one group are alternative renders of ONE block. When
	# the blob carries any of them, it is authoritative for all of them — a slot
	# it left unset is CLEARED, so a replayed render never stacks on a live one.
	persisted_groups = {
		layer.group for layer in FOUNDATION_LAYERS
		if layer.group and valid.get(layer.key) is not None
	}
	for layer in FOUNDATION_LAYERS:
		key, attr, tokens_attr, origin = layer.key, layer.attr, layer.tokens_attr, layer.origin
		if key == "worker_catalog":
			from agents.task.agent.profile_store import workers_enabled
			if not workers_enabled():
				planned.append((attr, tokens_attr, None))
				continue
		content = valid.get(key)
		if content is None:
			if layer.group and layer.group in persisted_groups:
				planned.append((attr, tokens_attr, None))
			continue  # slot was unset when saved — keep whatever is live
		current = getattr(manager, attr, None)
		if not content:
			planned.append((attr, tokens_attr, None))
			continue
		if isinstance(content, list):
			planned.append((attr, tokens_attr, tuple(
				HumanMessage(content=c, origin=origin or MessageOrigin.USER)
				for c in content)))
			continue
		planned.append((
			attr, tokens_attr,
			_rebuild_message(current, content, origin, attr == "_system_message"),
		))

	for attr, tokens_attr, message in planned:
		setattr(manager, attr, message)
		tokens = 0
		if message is not None:
			try:
				if isinstance(message, tuple):
					tokens = sum(manager._count_message_tokens(m) for m in message)
				else:
					tokens = manager._count_message_tokens(message)
			except Exception:  # pragma: no cover - defensive
				tokens = 0
		setattr(manager, tokens_attr, tokens)

	# ⚠️ Do NOT touch ``_tool_catalog_source``. It holds the RAW catalog text the
	# setter was last handed, while the persisted slot holds the ENVELOPED message
	# content; writing the envelope there would make the very next catalog refresh
	# read as a change and rewrite (F8: push a tail delta for) the block this
	# replay just restored. Left alone it still equals the live render, so an
	# unchanged catalog is the no-op it should be.

	manager._foundation_tool_names = list(valid.get("tool_names") or []) or None
	log.info(
		"📂 Replayed the persisted foundation (%d slots, %d tool names): the prompt "
		"prefix survives the restart",
		len(planned), len(manager._foundation_tool_names or []))
	return True
