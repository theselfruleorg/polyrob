"""The Anthropic wire: one message conversion, the thinking blocks, a refusal.

⚠️ NOT LIVE-TESTED. This tree has no Anthropic key, so the replay of a thinking block, the
``block_binding`` control and ``output_config.effort`` have never gone to the real API. They
fail soft: a 400 that names them stops them on this client and the request goes once without.

Thinking blocks, per the preserved-thinking docs:

* A signed block replays unchanged, in its position before the ``tool_use`` it preceded. The
  agent rewrites the assistant text in its history, and a block's record holds only what came
  before it, so a first block stays valid. A later block of a turn with changed text does not.
* The record of a block holds the system prompt, the tools and every earlier message. This
  agent's transcript is not append-only: the state row and the H-MEM row ride one call and
  leave, ageing edits old results, and compaction rebuilds rows with no blocks. So a model
  with the check gets ``prefix_mismatch_behavior: drop_block``: the API drops what no longer
  matches, does not bill it, and the request succeeds. ``input_transformations`` is logged.
* The blocks ride ``additional_kwargs['anthropic_thinking']``. The agent keeps the key through
  its history and its session files, and no other provider's adapter reads it.
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.exceptions import LLMInvalidRequestError, LLMRefusalError

logger = logging.getLogger(__name__)

BETA_HEADER_NAME = "anthropic-beta"
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
_THINKING_KEY = "thinking_blocks"
_THINKING_TYPES = ("thinking", "redacted_thinking")
_UNAVAILABLE_ATTR = "_polyrob_thinking_controls_unavailable"
#: A refusal of the thinking controls holds this long, not for the process: its markers can
#: also match a 400 that one session's history caused (a swap mid tool loop).
LATCH_TTL_SEC = 3600.0
_REFUSAL_MARKERS = ("block_binding", THINKING_BINDING_BETA, "output_config", "`signature`",
                    "thinking block", "`thinking` block")
#: A 400 that names the tools array: the one request shape that a call with no tools cures.
_TOOL_SCHEMA_RE = re.compile(r"\btools\.\d+|\binput_schema\b")


def is_http_400(exc: Any) -> bool:
    """Is *exc* an HTTP 400? Deliberately duck-typed: the SDK's error class is not importable
    from this module (the ``anthropic`` package is an extra), and a wrapped/translated error
    must still be recognised."""
    if exc is None:
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    if status is None:
        return "BadRequest" in type(exc).__name__
    return int(status) == 400


def with_beta(api_params: Dict[str, Any], beta: str) -> Dict[str, Any]:
    """Merge *beta* into ``extra_headers`` without losing an existing value."""
    headers = dict(api_params.get("extra_headers") or {})
    existing = headers.get(BETA_HEADER_NAME)
    if existing and beta not in existing:
        headers[BETA_HEADER_NAME] = f"{existing},{beta}"
    elif not existing:
        headers[BETA_HEADER_NAME] = beta
    api_params["extra_headers"] = headers
    return api_params


def without_beta(api_params: Dict[str, Any], beta: str) -> Dict[str, Any]:
    """Drop *beta* from ``extra_headers`` (and the key if empty)."""
    headers = api_params.get("extra_headers")
    if not isinstance(headers, dict) or BETA_HEADER_NAME not in headers:
        return api_params
    kept = [v for v in str(headers[BETA_HEADER_NAME]).split(",")
            if v.strip() and v.strip() != beta]
    headers = dict(headers)
    if kept:
        headers[BETA_HEADER_NAME] = ",".join(kept)
    else:
        headers.pop(BETA_HEADER_NAME)
    if headers:
        api_params["extra_headers"] = headers
    else:
        api_params.pop("extra_headers", None)
    return api_params


def is_tool_schema_error(error: BaseException, translated: BaseException) -> bool:
    return isinstance(translated, LLMInvalidRequestError) and bool(_TOOL_SCHEMA_RE.search(str(error)))


# --- the response ---------------------------------------------------------------------

def raise_on_refusal(response: Any, provider: str, model: Optional[str]) -> None:
    """``stop_reason: refusal`` is a normal HTTP 200 with no usable content. A retry gets the
    same answer, so it raises typed and permanent with the category, which the owner reads."""
    if getattr(response, "stop_reason", None) != "refusal":
        return
    details = getattr(response, "stop_details", None)
    category = details.get("category") if isinstance(details, dict) else getattr(details, "category", None)
    raise LLMRefusalError(
        f"{provider}: model {model} declined this request (category: {category or 'not given'}). "
        "The same request gets the same answer.", category=category, kind="refusal")


def note_input_transformations(response: Any) -> None:
    """What the API dropped from this request. The list is empty on an append-only turn."""
    entries = getattr(response, "input_transformations", None)
    if isinstance(entries, list) and entries:
        reasons = sorted({str(e.get("reason") if isinstance(e, dict) else getattr(e, "reason", "")) for e in entries})
        logger.info("event=thinking_dropped blocks=%d reasons=%s", len(entries), reasons)


def thinking_record(response: Any) -> Optional[List[Dict[str, Any]]]:
    """The response's thinking blocks with their positions, as the list the agent keeps."""
    content = getattr(response, "content", None)
    if not isinstance(content, (list, tuple)):
        return None
    blocks: List[Dict[str, Any]] = []
    waiting: List[int] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "thinking":
            blocks.append({"block": {"type": "thinking", "thinking": getattr(block, "thinking", ""),
                                     "signature": getattr(block, "signature", "")}})
            waiting.append(len(blocks) - 1)
        elif kind == "redacted_thinking":
            blocks.append({"block": {"type": "redacted_thinking", "data": getattr(block, "data", "")}})
            waiting.append(len(blocks) - 1)
        elif kind == "tool_use":
            for index in waiting:
                blocks[index]["before_tool"] = getattr(block, "id", None)
            waiting = []
    return blocks or None


# --- the request ----------------------------------------------------------------------

def _tool_call_fields(call: Dict[str, Any]) -> Tuple[str, Any, str]:
    """(id, name, input object) of a tool call in the OpenAI or the flat shape."""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name") or call.get("name") or ""
    raw = function.get("arguments") if function else call.get("args", call.get("arguments"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            logger.warning("tool call %s holds arguments that are not JSON", name)
            raw = {"raw": raw}
    if raw is None:
        raw = {}
    return call.get("id", ""), name, raw if isinstance(raw, dict) else {"value": raw}


def _assistant_blocks(msg: Dict[str, Any], content: Any, keep_thinking: bool) -> Any:
    """An assistant row as blocks: thinking in its position, text, ``tool_use``. A block that
    came after all tool calls of a turn has no place to go and leaves."""
    calls = msg.get("tool_calls") or []
    thinking = (msg.get(_THINKING_KEY) or []) if keep_thinking else []
    if not calls and not thinking:
        return content
    use_ids = [_tool_call_fields(c)[0] for c in calls]
    before: Dict[Any, List[Dict[str, Any]]] = {}
    for item in thinking:
        before.setdefault(item.get("before_tool"), []).append(item["block"])
    first = use_ids[0] if use_ids else None
    blocks: List[Dict[str, Any]] = list(before.get(first if calls else None, []))
    if isinstance(content, list):
        blocks.extend(content)
    elif content:
        blocks.append({"type": "text", "text": content})
    for call, call_id in zip(calls, use_ids):
        if call_id != first:
            blocks.extend(before.get(call_id, []))
        _id, name, args = _tool_call_fields(call)
        blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
    return blocks


def _flat_tool_calls(msg: Dict[str, Any], content: Any) -> Any:
    """The no-tools shape of an assistant turn: its calls as text, since a request with no
    ``tools`` may hold no ``tool_use``."""
    calls = msg.get("tool_calls") or []
    if not calls:
        return content
    notes = [f"[called {name} with {json.dumps(args, sort_keys=True, default=str)}]"
             for _id, name, args in map(_tool_call_fields, calls)]
    text = "\n".join(notes)
    return f"{content}\n{text}" if isinstance(content, str) and content else text


def wire_messages(messages: List[Dict[str, Any]], system: Optional[Any], *, tool_blocks: bool,
                  keep_thinking: bool = False) -> Tuple[List[Dict[str, Any]], Optional[Any]]:
    """(wire rows, system) of a request, for the tool path and the no-tools path.

    An explicit ``system`` wins, else the first system row, which is where the agent builds it.
    Consecutive tool results go in ONE user message, because split results teach Claude to
    stop making parallel calls. With ``tool_blocks`` off a result is labeled user text.
    """
    rows: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            if system is None and isinstance(content, str) and content:
                system = content
            continue
        if role == "tool":
            if tool_blocks:
                block = {"type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""), "content": content}
                if rows and is_tool_result_row(rows[-1]):
                    rows[-1]["content"].append(block)
                else:
                    rows.append({"role": "user", "content": [block]})
            else:
                text = f"[tool result] {content}" if isinstance(content, str) else content
                rows.append({"role": "user", "content": text})
            continue
        if role in ("assistant", "ai"):
            body = (_assistant_blocks(msg, content, keep_thinking) if tool_blocks
                    else _flat_tool_calls(msg, content))
            if body:  # the API refuses an assistant row with no content
                rows.append({"role": "assistant", "content": body})
            continue
        if role != "user":
            logger.warning("Unknown role %s, treating as user", role)
        rows.append({"role": "user", "content": content})
    return rows, system


def is_tool_result_row(row: Any) -> bool:
    content = row.get("content") if isinstance(row, dict) else None
    return (isinstance(row, dict) and row.get("role") == "user" and isinstance(content, list) and bool(content)
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))


def wire_metadata(metadata: Any) -> Optional[Dict[str, str]]:
    """The API's ``metadata`` holds only ``user_id``. The adapter's telemetry keys stay local."""
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    return {"user_id": user_id} if isinstance(user_id, str) and user_id else None


def thinking_controls_unavailable(client: Any) -> bool:
    at = getattr(client, _UNAVAILABLE_ATTR, None)
    return isinstance(at, float) and time.monotonic() - at < LATCH_TTL_SEC


def apply_thinking_controls(api_params: Dict[str, Any], caps: Any) -> None:
    """The row's effort, and ``drop_block`` on a row with the history check."""
    effort = getattr(caps, "reasoning_effort", None)
    if effort:
        api_params["output_config"] = {"effort": effort}
    if getattr(caps, "preserves_thinking", False):
        api_params["thinking"] = {"type": "adaptive",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        with_beta(api_params, THINKING_BINDING_BETA)


def retry_params_without_thinking_controls(client: Any, exc: Any, api_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The request with no thinking control and no thinking block, or None to re-raise.

    A 400 that names them stops them on this client, so later requests send the same history.
    """
    if not is_http_400(exc) or not any(m in str(exc) for m in _REFUSAL_MARKERS):
        return None
    has_block = any(isinstance(b, dict) and b.get("type") in _THINKING_TYPES
                    for row in api_params.get("messages") or [] if isinstance(row, dict)
                    and isinstance(row.get("content"), list) for b in row["content"])
    if not has_block and not ({"thinking", "output_config"} & set(api_params)):
        return None
    setattr(client, _UNAVAILABLE_ATTR, time.monotonic())
    logger.warning("Anthropic refused the thinking controls; retrying with none and not "
                   "sending them again on this client: %s", exc)
    out = {k: v for k, v in api_params.items() if k not in ("thinking", "output_config")}
    without_beta(out, THINKING_BINDING_BETA)
    out["messages"] = [
        {**row, "content": [b for b in row["content"]
                            if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)]}
        if isinstance(row, dict) and isinstance(row.get("content"), list) else row
        for row in api_params.get("messages") or []]
    return out

