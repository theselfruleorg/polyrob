"""Generic OpenAI **Responses API** client (proposal 024, L2 transport gap).

The third wire format, alongside chat-completions and Anthropic messages. It
exists because subscription seats served through OpenAI's Codex backend
(`openai-codex`) speak Responses and nothing else — before this, connecting a
ChatGPT plan stored a credential that inference could never use.

Implemented against the Responses wire contract directly; the format's sharp
edges below are cheap to learn and expensive to rediscover:

- **Tool schemas are FLAT.** Chat-completions nests under ``function``;
  Responses puts ``name``/``description``/``parameters`` at the top level of a
  ``{"type": "function"}`` entry. Sending the nested shape is accepted and the
  model then never calls a tool — the silent tools=0 failure.
- **Text part types are role-dependent.** A user message carries
  ``input_text``; an assistant message carries ``output_text``. The API rejects
  each in the other's position.
- **Tool calls and results are TOP-LEVEL items**, not message fields:
  ``{"type": "function_call", call_id, name, arguments}`` and
  ``{"type": "function_call_output", call_id, output}``.
- **``system`` is not a role here** — it is passed as ``instructions``.
- Item ids longer than 64 chars are rejected outright, so replayed
  server-assigned ids are dropped rather than echoed.

Only the three methods the adapter contract needs are overridden, so the
existing ``OpenRouterAdapter`` drives this unchanged.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from modules.llm.compat_clients import OpenAICompatClient

#: The Responses API rejects an input item id longer than this with a
#: non-retryable 400. Server-assigned ids can be far longer, so a replayed one
#: is dropped instead of echoed back.
MAX_ITEM_ID_LENGTH = 64


def to_responses_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Chat-completions tool schemas → Responses function-tool schemas.

    The shape is FLAT: ``{"type": "function", "name": ..., "parameters": ...}``.
    Passing the chat-completions ``{"function": {...}}`` nesting is accepted by
    the endpoint and then silently ignored, which presents as a model that never
    calls a tool rather than as an error.
    """
    if not tools:
        return None
    out: List[Dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = fn.get("name") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or item.get("description") or "",
            # strict=True would demand a fully-closed JSON schema; our tool
            # schemas are not authored to that standard and would be rejected.
            "strict": False,
            "parameters": (fn.get("parameters") or item.get("parameters")
                           or {"type": "object", "properties": {}}),
        })
    return out or None


def _text_parts(content: Any, role: str) -> List[Dict[str, Any]]:
    """Message content → Responses content parts, with the role-correct type."""
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    parts: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text"):
                text = part.get("text")
                if text:
                    parts.append({"type": text_type, "text": text})
            elif ptype in ("image_url", "input_image"):
                url = part.get("image_url", {})
                url = url.get("url") if isinstance(url, dict) else url
                if url:
                    parts.append({"type": "input_image", "image_url": url})
    return parts


def to_responses_input(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """Chat messages → ``(input_items, instructions)``.

    ``system`` is NOT a role in this API — it is hoisted into the top-level
    ``instructions`` parameter. Assistant tool calls and tool results become
    TOP-LEVEL items rather than fields on a message.
    """
    items: List[Dict[str, Any]] = []
    instructions: List[str] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str) and content:
                instructions.append(content)
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                items.append({
                    "type": "function_call_output",
                    "call_id": str(call_id),
                    "output": content if isinstance(content, str) else json.dumps(content),
                })
            continue

        if role == "assistant":
            parts = _text_parts(content, "assistant")
            if parts:
                item: Dict[str, Any] = {
                    "type": "message", "role": "assistant", "content": parts,
                }
                msg_id = msg.get("id")
                # Only keep an id short enough to be legal; a long
                # server-assigned one is a hard 400.
                if isinstance(msg_id, str) and 0 < len(msg_id) <= MAX_ITEM_ID_LENGTH:
                    item["id"] = msg_id
                items.append(item)
            for call in msg.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                name = fn.get("name")
                if not name:
                    continue
                args = fn.get("arguments")
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or f"call_{len(items)}"),
                    "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                })
            continue

        parts = _text_parts(content, "user")
        if parts:
            items.append({"role": "user", "content": parts})

    return items, "\n\n".join(instructions)


def from_responses_output(response: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Responses output items → ``(text, tool_calls)`` in chat-completions shape.

    Tool calls are re-nested under ``function`` because that is what the rest of
    the stack (ToolCallBuilder → ToolCallTracker → Registry) consumes.
    """
    text_chunks: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for item in getattr(response, "output", None) or []:
        itype = getattr(item, "type", None)
        if itype == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) in ("output_text", "text"):
                    chunk = getattr(part, "text", None)
                    if chunk:
                        text_chunks.append(chunk)
        elif itype == "function_call":
            name = getattr(item, "name", None)
            if not name:
                continue
            tool_calls.append({
                "id": str(getattr(item, "call_id", None) or getattr(item, "id", "") or
                          f"call_{len(tool_calls)}"),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": getattr(item, "arguments", None) or "{}",
                },
            })
        # `reasoning` items are deliberately dropped: they are the model's
        # scratchpad, and letting them into content is exactly what the
        # think-scrubber exists to prevent.

    return "".join(text_chunks).strip(), tool_calls


def _usage_from(response: Any) -> Dict[str, Optional[int]]:
    """Responses usage → the adapter's usage dict.

    Responses names them input/output; the rest of the stack counts
    prompt/completion.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "output_tokens", None)
    total = getattr(usage, "total_tokens", None)
    cached = None
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
    out: Dict[str, Optional[int]] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else (
            (prompt or 0) + (completion or 0) if (prompt or completion) else None),
    }
    if cached:
        out["cached_tokens"] = cached
    return out


class ResponsesCompatClient(OpenAICompatClient):
    """OpenAI Responses-API client driven by a ProviderSpec.

    Rides ``OpenAICompatClient`` for construction, credential resolution and
    base-URL handling, and replaces only the two generation methods the adapter
    contract calls — so ``OpenRouterAdapter`` drives it with no changes.
    """

    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]]:
        """The native tool-calling path. Returns the canonical 3-tuple."""
        await self._ensure_client()
        items, instructions = to_responses_input(messages)
        system = kwargs.get("system")
        if system:
            instructions = f"{system}\n\n{instructions}".strip()

        params: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model_type,
            "input": items,
        }
        if instructions:
            params["instructions"] = instructions
        converted = to_responses_tools(tools)
        if converted:
            params["tools"] = converted
        max_tokens = kwargs.get("max_tokens") or getattr(self, "max_tokens", None)
        if max_tokens:
            params["max_output_tokens"] = max_tokens

        response = await self._client.responses.create(**params)
        self.last_response = response
        content, tool_calls = from_responses_output(response)
        return content, tool_calls, _usage_from(response)

    async def generate_response(self, messages: List[Dict[str, Any]], **kwargs) -> str:
        """The no-tools path. Returns text only."""
        content, _calls, _usage = await self.generate_agent_response(
            messages, tools=[], **kwargs
        )
        return content

    async def _ensure_client(self) -> None:
        if self._client is None:
            await self._setup_client()
