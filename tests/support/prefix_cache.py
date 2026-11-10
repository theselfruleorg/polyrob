"""F21 — the prefix-stability harness.

Every provider that caches a prompt caches a PREFIX: request N+1 is served from
cache only for the leading bytes it shares with request N. Nothing in the tree
asserted that two consecutive in-session assemblies share one. This module is
the measuring instrument the tests use:

* :func:`serialize_messages` — one canonical JSON string per message, so two
  assemblies can be compared position by position without caring about object
  identity.
* :func:`common_prefix_len` — how many leading messages two assemblies share.
* :class:`PrefixCacheProbe` — records successive ``get_messages_for_llm()``
  snapshots and reports the hit ratio (shared prefix / previous length), the
  same number a faux provider would report.

* :func:`wire_adapter` — the REAL adapter over the REAL client with a fake SDK, so a
  test reads the request that would go on the wire, ``tools[]`` included.
* :func:`wire_rows` / :func:`wire_prefix_len` — that request as the provider caches
  it: the tools come first, then one row per message.

Dependency-free at import (stdlib only), so it can be imported from any test without
dragging the agent tier in. :func:`wire_adapter` imports the LLM layer when it runs.
"""

from __future__ import annotations

import json
import logging
import types
from typing import Any, List, Optional, Sequence


def _jsonable(value: Any) -> Any:
	"""Best-effort JSON-safe projection of a message field."""
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, dict):
		return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
	if isinstance(value, (list, tuple)):
		return [_jsonable(v) for v in value]
	return str(value)


def serialize_message(message: Any) -> str:
	"""Canonical JSON for ONE message.

	The four fields that reach the provider wire and therefore decide a cache
	hit: the role/type, the content, the tool calls and the tool_call_id.
	Anything else (in-process ``origin``, metadata, object identity) is
	deliberately excluded — it never reaches the provider.
	"""
	payload = {
		"type": getattr(message, "type", None) or type(message).__name__,
		"content": _jsonable(getattr(message, "content", None)),
		"tool_calls": _jsonable(getattr(message, "tool_calls", None) or None),
		"tool_call_id": _jsonable(getattr(message, "tool_call_id", None)),
	}
	return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def serialize_messages(msgs: Sequence[Any]) -> List[str]:
	"""One canonical JSON string per message, in assembly order."""
	return [serialize_message(m) for m in msgs]


def common_prefix_len(a: Sequence[str], b: Sequence[str]) -> int:
	"""Count of leading EQUAL messages shared by two serialized assemblies."""
	n = 0
	for left, right in zip(a, b):
		if left != right:
			break
		n += 1
	return n


class _FakeCompletions:
	"""The fake SDK endpoint: records each request and answers with ``text``."""

	def __init__(self, text: str):
		self.calls: List[dict] = []
		self.text = text

	async def create(self, **params: Any) -> Any:
		self.calls.append(params)
		message = types.SimpleNamespace(content=self.text, tool_calls=None)
		choice = types.SimpleNamespace(message=message, finish_reason="stop")
		return types.SimpleNamespace(choices=[choice], usage=None, id="gen-wire")


def wire_adapter(model: str = "deepseek/deepseek-v4.1-flash", text: str = "ok") -> Any:
	"""A real ``OpenRouterAdapter`` over a real ``OpenRouterClient`` with a fake SDK.

	``adapter.wire_calls()`` returns the keyword arguments of each
	``chat.completions.create``: the request that would reach the provider.
	"""
	from modules.llm.adapters import OpenRouterAdapter
	from modules.llm.openrouter_client import OpenRouterClient

	completions = _FakeCompletions(text)
	client = OpenRouterClient.__new__(OpenRouterClient)
	client.model_type = model
	client.max_tokens = 128
	client.temperature = 0.2
	client.supports_vision = False
	client.last_response = None
	client._initialized = True
	client._PROVIDER_LABEL = "OpenRouter"
	client.logger = logging.getLogger("tests.wire_client")
	client._client = types.SimpleNamespace(
		chat=types.SimpleNamespace(completions=completions))
	client._client.with_options = lambda **_kwargs: client._client
	adapter = OpenRouterAdapter(client=client, model_name=model)
	object.__setattr__(adapter, "wire_calls", lambda: completions.calls)
	return adapter


def wire_rows(request: dict) -> List[str]:
	"""A wire request as the provider caches it: ``tools[]``, then each message."""
	rows = [json.dumps(_jsonable(request.get("tools") or []), sort_keys=True, ensure_ascii=False)]
	rows.extend(json.dumps(_jsonable(m), sort_keys=True, ensure_ascii=False)
	            for m in request.get("messages") or [])
	return rows


def wire_prefix_len(a: dict, b: dict) -> int:
	"""Leading rows two wire requests share. 0 means the tools differ."""
	return common_prefix_len(wire_rows(a), wire_rows(b))


class PrefixCacheProbe:
	"""Record successive assemblies and report what a prompt cache would hit.

	Usage::

		probe = PrefixCacheProbe()
		for _ in range(6):
			...
			probe.record(mm.get_messages_for_llm(consume_ephemeral=False))
		assert probe.hit_ratio == 1.0

	``hit_ratio`` is the LAST recorded snapshot's ratio: shared leading
	messages / the PREVIOUS snapshot's length. The first snapshot has no
	predecessor and reports ``0.0`` (nothing could have been cached).
	"""

	__slots__ = ("snapshots", "ratios", "prefixes")

	def __init__(self) -> None:
		self.snapshots: List[List[str]] = []
		self.ratios: List[float] = []
		self.prefixes: List[int] = []

	def record(self, messages: Sequence[Any]) -> List[str]:
		"""Record one assembly; returns its serialized form."""
		snapshot = serialize_messages(messages)
		if self.snapshots:
			previous = self.snapshots[-1]
			shared = common_prefix_len(previous, snapshot)
			self.prefixes.append(shared)
			self.ratios.append(shared / len(previous) if previous else 0.0)
		else:
			self.prefixes.append(0)
			self.ratios.append(0.0)
		self.snapshots.append(snapshot)
		return snapshot

	@property
	def hit_ratio(self) -> float:
		"""The most recent snapshot's shared-prefix ratio (0.0 with < 2 records)."""
		return self.ratios[-1] if len(self.ratios) > 1 else 0.0

	@property
	def last_prefix_len(self) -> int:
		"""Shared leading message count of the most recent pair."""
		return self.prefixes[-1] if self.prefixes else 0

	def previous_len(self) -> int:
		"""Length of the snapshot before the most recent one (0 with < 2)."""
		return len(self.snapshots[-2]) if len(self.snapshots) > 1 else 0

	def first_divergence(self) -> Optional[int]:
		"""Index of the first message where the last two snapshots differ.

		``None`` when the newer assembly extends the older one exactly.
		"""
		if len(self.snapshots) < 2:
			return None
		previous, current = self.snapshots[-2], self.snapshots[-1]
		shared = common_prefix_len(previous, current)
		if shared == len(previous):
			return None
		return shared

	def describe_last(self) -> str:
		"""Human-readable diagnosis for an assertion message."""
		if len(self.snapshots) < 2:
			return "only one snapshot recorded"
		previous, current = self.snapshots[-2], self.snapshots[-1]
		idx = self.first_divergence()
		if idx is None:
			return f"prefix intact: {len(previous)} -> {len(current)} messages"
		old = previous[idx] if idx < len(previous) else "<missing>"
		new = current[idx] if idx < len(current) else "<missing>"
		return (
			f"prefix broke at index {idx} of {len(previous)} "
			f"(kept {self.last_prefix_len})\n  was: {old[:240]}\n  now: {new[:240]}"
		)
