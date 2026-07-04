"""Shared brain-state scrubber (OR-7, 2026-06).

POLYROB instructs every model to emit its brain-state as a ``{"current_state": {...}}``
JSON object (or bare brain keys) in the text-content field every turn. That content
is internal telemetry, NOT the agent's user-facing voice. On a streaming provider it
reaches the single stream funnel (``hitl_manager.stream_output`` → CLI / WebView /
API), where the old all-or-nothing guard only suppressed a chunk that parsed *purely*
as a brain object. Real leak shapes therefore streamed to users:

  - fenced ```json {brain} ```            (DeepSeek)
  - mixed blob: {brain} + real prose       (DeepSeek)
  - prose + trailing {brain}               (Qwen / others)
  - truncated brain JSON                    (Qwen — stream cut mid-object)
  - {brain} + trailing tool-call junk      (Kimi)

``scrub_brain_blocks`` removes the brain block(s) wherever they appear and returns
the remaining prose. It is the single detector both the stream funnel and the CLI
render layer should share. Pure, dependency-free, fail-open (on any internal error a
caller should fall back to streaming rather than dropping a real reply).
"""
from __future__ import annotations

import json
import re
from typing import Optional

__all__ = ["scrub_brain_blocks", "looks_like_brain_block", "BRAIN_KEYS"]

#: Keys that mark a dict as agent brain-state rather than an arbitrary JSON result.
#: A ``current_state`` wrapper OR >= MIN_BRAIN_KEYS of these ⇒ brain-state.
BRAIN_KEYS = frozenset(
    {
        "current_state",
        "next_goal",
        "evaluation_previous_goal",
        "page_summary",
        "memory",
        "reasoning",
        "macro_goal",
        "subgoal",
        "phase",
    }
)
_MIN_BRAIN_KEYS = 2

#: Kimi-K2 tool-call control tokens (NVIDIA NIM intermittently leaks these as text).
_KIMI_CONTROL_TOKEN_RE = re.compile(
    r"<\|(?:tool_call_begin|tool_call_end|tool_call_argument_begin|tool_calls_section_end)\|>"
)

#: A fenced code block: ```lang\n …body… ``` (or ~~~). Non-greedy body.
_FENCE_BLOCK_RE = re.compile(r"(?:```|~~~)[^\n`~]*\n(.*?)(?:```|~~~)", re.DOTALL)


def _is_brain_obj(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("current_state"), dict):
        return True
    return sum(1 for k in obj if k in BRAIN_KEYS) >= _MIN_BRAIN_KEYS


def _looks_brainish(fragment: str) -> bool:
    """Heuristic for a TRUNCATED brain object: enough brain-key labels present."""
    if '"current_state"' in fragment:
        return True
    return sum(1 for k in BRAIN_KEYS if f'"{k}"' in fragment) >= _MIN_BRAIN_KEYS


def _inner_is_brain(inner: str) -> bool:
    inner = inner.strip()
    if not inner.startswith("{"):
        return False
    try:
        obj, _end = json.JSONDecoder().raw_decode(inner)
        return _is_brain_obj(obj)
    except ValueError:
        return _looks_brainish(inner)


def looks_like_brain_block(text: Optional[str]) -> bool:
    """True when *text*, after scrubbing, has no remaining prose — i.e. it is
    (wholly) brain-state. The shared replacement for the divergent
    ``dialog.is_brain_state`` / ``utils_json.is_brain_state_content`` checks."""
    if not text:
        return False
    return scrub_brain_blocks(text).strip() == ""


def scrub_brain_blocks(text: Optional[str]) -> Optional[str]:
    """Strip brain-state JSON block(s) from *text*, returning the prose remainder.

    Returns the input unchanged (no allocation surprises) when it carries no
    ``{`` at all. Genuine prose and genuine non-brain JSON are never altered.
    """
    if not text or "{" not in text:
        return text

    s = _KIMI_CONTROL_TOKEN_RE.sub("", text)

    # 1) Drop fenced regions whose body is brain-shaped (keeps non-brain code fences).
    def _fence_sub(m: "re.Match[str]") -> str:
        return "" if _inner_is_brain(m.group(1)) else m.group(0)

    s = _FENCE_BLOCK_RE.sub(_fence_sub, s)

    # 2) Walk the remainder, removing each bare brain object (complete or truncated).
    decoder = json.JSONDecoder()
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "{":
            try:
                obj, end = decoder.raw_decode(s, i)
            except ValueError:
                # Unparseable object here: a truncated brain tail (stream cut
                # mid-object) ⇒ drop to end; otherwise keep one char and advance.
                if _looks_brainish(s[i:]):
                    break
                out.append(s[i])
                i += 1
                continue
            if _is_brain_obj(obj):
                i = end  # skip the brain object entirely
                continue
            out.append(s[i:end])  # genuine non-brain JSON — keep verbatim
            i = end
            continue
        out.append(s[i])
        i += 1

    result = "".join(out)
    if result == s and s == text:
        return text  # nothing removed and no token-strip ⇒ exact input
    # Something was removed (or tokens stripped): tidy leftover whitespace.
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
