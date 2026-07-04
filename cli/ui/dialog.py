"""dialog.py — pure predicates for the dialog-first CLI rendering.

The POLYROB agent talks to the user via the ``send_message`` TOOL.  The CLI must
render that tool's ``text`` param as a readable chat message ("the hero") and
DEMOTE the telemetry plumbing around it (the post-hoc ``Executed: …`` reasoning
echo, the ``send_message(...)→…`` memory echo, the ``Message sent to user. …``
plumbing receipt that flows through ``on_turn_end``).

This module centralises every string-literal predicate so the renderers never
scatter them.  Everything here is PURE (no I/O, no state) and trivially
unit-testable.

Action shape (real captured feed, §0 amendment 3):
    {action_type, name, service, params{...}}
A send_message action has ``action_type == "send_message"`` (primary match) and
``name in {"message", "send_message"}`` (fallback).  ``params.text`` carries the
full, UNtruncated message the agent wants the user to read.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from cli.ui.theme import fmt_tokens


def summary_segments(
    *,
    steps: int = 0,
    tools: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    elapsed_seconds: float = 0.0,
    failed: bool = False,
) -> List[str]:
    """The ordered turn-summary segments (SSOT for the activity residue line).

    ``["3 steps", "2 tools", "14.2k tok", "$0.0040", "28s"]`` — zero-valued
    segments are omitted; cost below ``$0.00005`` (renders as ``$0.0000``) and
    elapsed below ``1s`` are dropped. Pure; consumed by both
    ``blocks.turn_summary_line`` (Rich) and ``PlainRenderer`` (plain) so the
    formatting rule lives in exactly one place.
    """
    parts: List[str] = []
    if steps:
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    if tools:
        parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
    if tokens:
        parts.append(f"{fmt_tokens(tokens)} tok")
    if cost >= 0.00005:  # below this it renders as $0.0000 — omit
        parts.append(f"${cost:.4f}")
    if elapsed_seconds >= 1.0:
        parts.append(f"{elapsed_seconds:.0f}s")
    if failed:
        parts.append("failed")
    return parts


#: OR-7: prefer the clean parsed action text (done/send_message) over the raw
#: streamed buffer at turn end — the stream is internal brain-state telemetry for
#: many providers; the parsed answer is the agent's real voice. Default ON; set
#: CLI_PREFER_ACTION_TEXT=off/false/0/no to restore the legacy stream-preference.
PREFER_ACTION_TEXT = os.getenv("CLI_PREFER_ACTION_TEXT", "true").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)

#: OR-1: in one-shot, when the agent already sent a reply via send_message and then
#: calls done() with a bookkeeping recap ("Responded to the greeting…"), the recap
#: renders as a confusing SECOND bubble. Suppress it (mirror REPL). Default ON; set
#: CLI_SUPPRESS_DONE_RECAP=off to restore the legacy double bubble.
SUPPRESS_DONE_RECAP = os.getenv("CLI_SUPPRESS_DONE_RECAP", "true").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)

#: A recap leads with bookkeeping narration about the turn just completed.
_RECAP_LEAD_RE = re.compile(
    r"^(responded|response|sent|greeted|replied|reported|acknowledged|"
    r"completed|task complete|done|finished|provided|delivered|no further|"
    r"i (?:have )?(?:responded|replied|greeted|sent|reported))\b",
    re.IGNORECASE,
)


def is_redundant_recap(answer: Optional[str], bubble_text: Optional[str]) -> bool:
    """True when *answer* is a bookkeeping recap of an already-rendered bubble
    rather than new user-facing content. Conservative — when unsure, returns False
    (never eat a genuine final answer)."""
    a = (answer or "").strip()
    b = (bubble_text or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    # Short meta-narration about the turn that just happened.
    return len(a) <= 200 and _RECAP_LEAD_RE.match(a) is not None

# ---------------------------------------------------------------------------
# Plumbing strings — the receipts the send_message tool returns to the agent.
# These are NOT the message; they must never be shown to the user as "rob".
# ---------------------------------------------------------------------------

#: The exact ``extracted_content`` strings the ``send_message`` action returns
#: (blocking + non-blocking) and the session-completion plumbing receipt.  When
#: ``on_turn_end`` is handed one of these it must render nothing.
_PLUMBING_STRINGS = frozenset(
    {
        "Message sent to user. Task paused - will resume when user responds.",
        "Message sent to user. Task paused — will resume when user responds.",
        "Message sent to user (non-blocking)",
        "Session completed successfully",
    }
)

#: Prefix of the post-hoc brain-state echo for native-tool turns
#: (e.g. ``Executed: send_message(text=…, wait_for_response=False)``).
_ECHO_REASONING_PREFIX = "Executed: "

#: Action names that mean "the agent is messaging the user".
_MESSAGE_NAMES = frozenset({"message", "send_message"})


# ---------------------------------------------------------------------------
# Plumbing / echo predicates
# ---------------------------------------------------------------------------


def is_plumbing_string(text: Optional[str]) -> bool:
    """True when *text* is one of the known send_message / session plumbing receipts."""
    if not text:
        return False
    return text.strip() in _PLUMBING_STRINGS


def choose_answer_text(answer: str, streamed: str) -> str:
    """OR-7: pick the answer text to render — parsed action text over raw stream.

    The streamed buffer is internal brain-state telemetry for many providers; the
    parsed ``answer`` (a done/send_message action's text) is the agent's real voice.
    Prefer ``answer`` when it's present and not a plumbing receipt; otherwise fall
    back to the stream (or ``answer`` when the stream is empty). SSOT for the
    identical selection both renderers' turn-end paths used to inline (D6).
    """
    ans = (answer or "").strip()
    if PREFER_ACTION_TEXT and ans and not is_plumbing_string(ans):
        return answer
    return streamed if streamed.strip() else answer


def is_echo_reasoning(text: Optional[str]) -> bool:
    """True when *text* is the post-hoc ``Executed: …`` brain-state echo."""
    if not text:
        return False
    return text.lstrip().startswith(_ECHO_REASONING_PREFIX)


def is_echo_memory(text: Optional[str]) -> bool:
    """True when a ``memory:`` line is just the action echo (hide by default).

    The native-tool memory line for a message turn looks like
    ``send_message(text=…, wait_for_response=False)→Message sent to user …``.
    We treat a memory line as an echo when it leads with a known message action
    name immediately followed by ``(`` (the call echo).
    """
    if not text:
        return False
    flat = text.strip()
    for name in _MESSAGE_NAMES:
        if flat.startswith(f"{name}("):
            return True
    return False


# ---------------------------------------------------------------------------
# send_message action extraction
# ---------------------------------------------------------------------------


def is_send_message_action(action: Dict[str, Any]) -> bool:
    """True when *action* is the agent messaging the user (send_message).

    Matches on ``action_type`` primarily, falling back to ``name`` (the real
    captured feed has ``action_type == "send_message"`` and ``name ==
    "message"``).
    """
    if not isinstance(action, dict):
        return False
    if action.get("action_type") == "send_message":
        return True
    return action.get("name") in _MESSAGE_NAMES


#: Action names that are the agent's DIALOG channel, not real tool work — they
#: render as the chat bubble / final answer and must NOT be double-rendered as a
#: tool call/result line in the tool transcript.
_DIALOG_ACTION_NAMES = frozenset({"message", "send_message", "done"})


def is_dialog_action_name(name: Optional[str]) -> bool:
    """True when *name* is a dialog-channel action (send_message/message/done).

    Used to exclude these from the tool transcript: a ``tool_execution`` event
    carries only ``action_name`` (no full action dict), so this name-level check
    is the seam for the result-line path (the start-line path uses
    ``is_send_message_action`` on the full action and a ``done`` check)."""
    if not name:
        return False
    return str(name).strip().lower() in _DIALOG_ACTION_NAMES


def message_text(action: Dict[str, Any]) -> Optional[str]:
    """Return the UNtruncated ``params.text`` of a send_message action, else None."""
    if not is_send_message_action(action):
        return None
    params = action.get("params")
    if not isinstance(params, dict):
        return None
    text = params.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def find_message_text(actions: List[Dict[str, Any]]) -> Optional[str]:
    """Return the first send_message ``params.text`` in *actions*, else None."""
    for action in actions or []:
        text = message_text(action)
        if text is not None:
            return text
    return None


def step_is_message_only(actions: List[Dict[str, Any]]) -> bool:
    """True when *actions* is non-empty and EVERY action is a send_message.

    A message-only step is a chat turn — the REPL renders just the bubble and
    skips the step header + scaffolding entirely.
    """
    acts = list(actions or [])
    if not acts:
        return False
    return all(is_send_message_action(a) for a in acts)


# ---------------------------------------------------------------------------
# Brain-state detection (planning-turn content — NOT a chat message)
# ---------------------------------------------------------------------------
#
# POLYROB allows a tool-free *planning turn* (``ALLOWED_REASONING_TURNS``).  On that
# turn there is no send_message, so the model's raw CONTENT — the brain state —
# is what streams into the live box.  That content is telemetry, not the agent's
# voice, and must be DEMOTED to a single dim "planning" line rather than printed
# as a "rob" bubble.  Some models (kimi-k2.6) format it as JSON, which otherwise
# surfaces as a raw ``{"current_state": …}`` dump.

#: Keys that mark a dict as an agent brain-state rather than an arbitrary JSON
#: result.  Two or more present (or a ``current_state`` wrapper) ⇒ brain-state.
_BRAIN_KEYS = frozenset(
    {
        "current_state",
        "next_goal",
        "evaluation_previous_goal",
        "page_summary",
        "memory",
        "reasoning",
        "macro_goal",
        "subgoal",
    }
)

#: Brain-state field values that carry no information for a planning line.
_BRAIN_PLACEHOLDERS = frozenset({"", "pending", "n/a", "none", "synthesis pending"})

#: Kimi-K2 tool-call control tokens (``<|tool_call_begin|>``, ``…_end|>``,
#: ``…_argument_begin|>``, ``<|tool_calls_section_end|>``) — all share the
#: ``tool_call`` lowercase/underscore prefix. NVIDIA NIM intermittently leaks
#: these as raw text. The LLM client strips them (WS-2.1); this is the render-
#: layer backstop (WS-3.2) so a leak via any other path still can't reach the
#: brain-state check or a "rob" bubble. Deliberately narrow — the exact closed set
#: of kimi's known tokens, never arbitrary ``<|…|>`` (or ``<|tool_call*|>`` lookalikes
#: from a future spec) that genuine prose might carry.
_KIMI_CONTROL_TOKEN_RE = re.compile(
    r"<\|(?:tool_call_begin|tool_call_end|tool_call_argument_begin|tool_calls_section_end)\|>"
)


def strip_control_tokens(text: Optional[str]) -> Optional[str]:
    """Remove leaked Kimi tool-call control tokens from *text*.

    No-op for content without them, so it's safe to apply to every provider's
    output before the brain-state / message checks.
    """
    if not text:
        return text
    return _KIMI_CONTROL_TOKEN_RE.sub("", text).strip()

#: Preference order for the one-line planning summary.
_PLANNING_FIELDS = ("next_goal", "reasoning", "memory")

#: A leading ```lang fenced block (markdown). DeepSeek (and other non-native
#: JSON-fallback providers) wrap their brain-state JSON in a ```json fence, which
#: makes the content start with backticks instead of "{" — defeating the JSON
#: brain-state check below and leaking the raw dump as a "rob" bubble (OR-2).
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)[^\n]*\n(.*?)\n?(?:```|~~~)\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Strip a single enclosing markdown code fence, else return *text* unchanged."""
    match = _CODE_FENCE_RE.match(text)
    return match.group(1).strip() if match else text


#: After a leading brain JSON object, only these trailing fragments still count as
#: "pure brain" (B4 leak residue): whitespace, Kimi control tokens, an XML
#: tool-call fragment (``<invoke …>`` / ``</invoke>``), or a single python-style
#: ``name(args)`` call. Anything else after the object is a GENUINE prose reply, so
#: the content is a mixed reply — NOT brain-state — and must not be demoted (OR-2/
#: FIX-C: prevents eating a real reply that follows a leaked brain dump).
_TRAILING_JUNK_RE = re.compile(
    r"^(?:\s*(?:</?invoke[^>]*>|</?function[^>]*>|[A-Za-z_]\w*\s*\([^)]*\)|<\|[^|]*\|>))*\s*$"
)


def _brain_from_json(text: str) -> Optional[Dict[str, Any]]:
    """Return the brain-state dict if *text* is brain-shaped JSON, else None.

    Uses ``raw_decode`` from the leading ``{`` rather than requiring the whole
    string to be one JSON object, so the B4 leak shapes — a brain JSON object
    followed by trailing tool-call junk (``… done(text=…)`` or ``… </invoke>``)
    that the client recovery couldn't fully strip — are still recognised as
    brain-state and DEMOTED to a planning line instead of dumped as a "rob"
    bubble. A leading ```json markdown fence (OR-2: DeepSeek non-native path) is
    stripped first so the fenced dump is recognised the same way.
    """
    flat = _strip_code_fence(text.strip())
    if not flat.startswith("{"):
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(flat)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    # FIX-C: if genuine prose follows the brain object, this is a MIXED reply, not
    # pure telemetry — don't classify it as brain-state (the demotion would eat the
    # real reply). Trailing tool-call junk / control tokens still count as brain.
    remainder = strip_control_tokens(flat[_end:]) or ""
    if remainder.strip() and not _TRAILING_JUNK_RE.match(remainder):
        return None
    inner = obj.get("current_state")
    if isinstance(inner, dict):
        return inner
    if sum(1 for k in obj if k in _BRAIN_KEYS) >= 2:
        return obj
    return None


def _brain_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Return brain fields if *text* is the ``Memory:/Next:/Reasoning:`` echo."""
    fields: Dict[str, Any] = {}
    for label, key in (
        ("memory", "memory"),
        ("next[_ ]?goal", "next_goal"),
        ("next", "next_goal"),
        ("reasoning", "reasoning"),
    ):
        if key in fields:
            continue
        match = re.search(rf"(?im)^\s*{label}\s*:\s*(.+?)\s*$", text)
        if match:
            fields[key] = match.group(1).strip()
    # Require both a memory and a next line so prose with a stray "Next:" header
    # isn't misread as brain-state.
    if "next_goal" in fields and "memory" in fields:
        return fields
    return None


def is_brain_state(text: Optional[str]) -> bool:
    """True when *text* is agent brain-state, not a chat message.

    Recognises the JSON shapes (``{"current_state": {...}}`` and bare brain
    keys) and the ``Memory:/Next:/Reasoning:`` text echo.  Genuine send_message
    bodies (markdown prose) and non-brain JSON return False.
    """
    if not text:
        return False
    # Strip any leaked Kimi control tokens first (WS-3.2) so the stray-end-token
    # shape ({brain json} <|tool_call_end|>) is still recognised as brain-state
    # rather than mis-rendered as a "rob" bubble.
    text = strip_control_tokens(text)
    if not text:
        return False
    return _brain_from_json(text) is not None or _brain_from_text(text) is not None


def brain_planning_line(text: Optional[str]) -> Optional[str]:
    """Distil brain-state *text* to one readable planning line, else None.

    Prefers ``next_goal``, falling through ``reasoning`` → ``memory`` past empty
    placeholders (``Pending``/``N/A``/``Synthesis pending``).  Returns None when
    *text* is not brain-state or carries no usable field.
    """
    if not text:
        return None
    text = strip_control_tokens(text)
    if not text:
        return None
    brain = _brain_from_json(text) or _brain_from_text(text)
    if not brain:
        return None
    for key in _PLANNING_FIELDS:
        val = brain.get(key)
        if isinstance(val, str) and val.strip().lower() not in _BRAIN_PLACEHOLDERS:
            return val.strip()
    return None
