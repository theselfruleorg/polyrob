"""Training-format renderers over the canonical TrajectoryRecord.

- ``raw``      — the canonical record as a dict (lossless).
- ``sharegpt`` — ShareGPT ``{"from","value"}`` turns using the
  ``<think>`` / ``<tool_call>`` / ``<tool_response>`` conventions (every gpt
  turn carries a — possibly empty — ``<think>`` block, tool calls serialize as
  ``{"name", "arguments"}`` JSON, tool results as ``{"tool_call_id", "name",
  "content"}``). Tool schemas are NOT embedded in the system turn (POLYROB
  does not persist per-session schemas); document consumers accordingly.
- ``openai``   — OpenAI messages shape with structured ``tool_calls`` /
  ``tool_call_id`` preserved.

Control-origin injected messages (SKILL / MEMORY / SELF_CONTEXT / …) render as
human/user turns — they ARE model input and the render stays faithful.
Labels + provenance ride alongside so corpus filters never re-parse content.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from datagen.record import TrajectoryRecord
from datagen.scrub import strip_images

logger = logging.getLogger(__name__)

_SHAREGPT_ROLES = {
    "SystemMessage": "system",
    "HumanMessage": "human",
    "AIMessage": "gpt",
    "ToolMessage": "tool",
}
_OPENAI_ROLES = {
    "SystemMessage": "system",
    "HumanMessage": "user",
    "AIMessage": "assistant",
    "ToolMessage": "tool",
}


def _flatten_content(content: Any) -> str:
    """Multimodal lists → newline-joined text with image placeholders."""
    content = strip_images(content)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _parse_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _tool_call_parts(msg: dict) -> list[dict]:
    """Normalize persisted tool_calls to ``{"id", "name", "arguments"}``."""
    parts = []
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name") or call.get("name") or ""
        args = _parse_arguments(fn.get("arguments", call.get("args", {})))
        parts.append({"id": call.get("id") or "",
                      "name": name, "arguments": args})
    return parts


def _metadata(record: TrajectoryRecord) -> dict:
    return {
        "session_id": record.session_id,
        "model": record.model,
        "provider": record.provider,
        "created_at": record.created_at,
        "exported_at": record.exported_at,
        "usage": record.usage,
        "provenance": record.provenance,
    }


def render_raw(record: TrajectoryRecord) -> dict:
    return record.to_dict()


def render_sharegpt(record: TrajectoryRecord) -> dict:
    call_names: dict[str, str] = {}
    conversations: list[dict] = []
    for msg in record.messages:
        if not isinstance(msg, dict):
            continue
        role = _SHAREGPT_ROLES.get(str(msg.get("type")), "human")
        text = _flatten_content(msg.get("content"))
        if role == "gpt":
            calls = _tool_call_parts(msg)
            for c in calls:
                if c["id"]:
                    call_names[c["id"]] = c["name"]
            value = text if text.lstrip().startswith("<think>") \
                else f"<think></think>\n{text}" if text else "<think></think>"
            for c in calls:
                value += ("\n<tool_call>\n"
                          + json.dumps({"name": c["name"],
                                        "arguments": c["arguments"]})
                          + "\n</tool_call>")
            conversations.append({"from": "gpt", "value": value})
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            payload = {"tool_call_id": call_id,
                       "name": call_names.get(call_id, ""),
                       "content": text}
            conversations.append({
                "from": "tool",
                "value": "<tool_response>\n" + json.dumps(payload)
                         + "\n</tool_response>",
            })
        else:
            conversations.append({"from": role, "value": text})
    return {"conversations": conversations,
            "labels": dict(record.labels),
            "metadata": _metadata(record)}


def render_openai(record: TrajectoryRecord) -> dict:
    messages: list[dict] = []
    for msg in record.messages:
        if not isinstance(msg, dict):
            continue
        role = _OPENAI_ROLES.get(str(msg.get("type")), "user")
        out: dict = {"role": role, "content": _flatten_content(msg.get("content"))}
        if role == "assistant":
            calls = _tool_call_parts(msg)
            if calls:
                out["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c["arguments"])
                                  if not isinstance(c["arguments"], str)
                                  else c["arguments"]}}
                    for c in calls]
        elif role == "tool":
            out["tool_call_id"] = str(msg.get("tool_call_id") or "")
        messages.append(out)
    return {"messages": messages,
            "labels": dict(record.labels),
            "metadata": _metadata(record)}


FORMATS = {
    "raw": render_raw,
    "sharegpt": render_sharegpt,
    "openai": render_openai,
}
