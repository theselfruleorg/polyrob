"""Control fences: the XML-ish blocks the runtime injects as CONTEXT (070 W1.5).

An ``<owner-thread>`` tail, a room's ``<group-context>``, a wrapped tool result —
each is context the agent is GIVEN for one call, never a finding of its own. When
the model copies such a block into its brain ``memory`` field, the memory write
path must not store it as a cross-session memory row.

Tier 0, stdlib only. This module STRIPS whole blocks (memory write path);
``core.surfaces.group_turn.defang`` is the different operation that NEUTRALIZES
a forged tag inside untrusted text that is still rendered.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

#: Every tag the runtime emits as an injected context fence (found by
#: ``grep -rhoE "<(owner-thread|group-context|…)[ >]" core agents modules tools``).
CONTROL_FENCE_TAGS = (
    "owner-thread",                 # core/surfaces/owner_thread.py render_block
    "group-context",                # core/surfaces/group_turn.py
    "addressed",                    # core/surfaces/group_turn.py (the member's ask)
    "untrusted_tool_result",        # core/security/untrusted_wrap.py
    "surface",                      # agents/task/agent/prompts.py
    "correspondent-message",        # agents/task/session/hitl_ingress.py
    "recalled-from-past-sessions",  # modules/llm/messages.py RECALL envelope
    "owner-instructions",           # agents/task/agent/prompts.py
    "memory-system",                # agents/task/agent/prompts.py
    "owner_answer",                 # agents/task/goals/rail_answers.py
    # modules/llm/messages.py _ORIGIN_ENVELOPE (core cannot import it; the
    # superset is pinned by tests/unit/core/test_context_fences.py)
    "system-directive",
    "approval-result",
    "session-memory",
    "available-skills",
    "available-tools",
    "worker-catalog",
    "self-context",
    "project-context",
    "system-note",
    "tool-notice",
    "compacted-history",
    "self-wake",
    "runtime-identity",
    "tool-addition",
    # other injected blocks
    "restart-note",                 # agents/task/runtime/run_as_session.py
    "live-health",                  # agents/task/agent/core/live_health.py
    "delegation-result",            # agents/task/agent/async_delegation.py
    "environment",                  # agents/task/agent/core/env_context.py
    "prior_summary_data",           # agents/task/agent/messages/compactor.py
    "tool-catalog",
    "skill-catalog",
    "tool-availability",
    "message-shape",
)

_ALT = "|".join(re.escape(t) for t in CONTROL_FENCE_TAGS)

# A whole block: an opening tag (bare or with attributes) through its closing tag.
_BLOCK_RE = re.compile(
    r"<\s*(%s)(?:\s[^>]*)?>.*?<\s*/\s*\1\s*>" % _ALT, re.IGNORECASE | re.DOTALL)
# A line that opens (or closes) a fence whose partner is missing.
_ORPHAN_LINE_RE = re.compile(
    r"^[ \t]*<\s*/?\s*(?:%s)(?:\s[^>\n]*)?>[^\n]*(?:\n|$)" % _ALT,
    re.IGNORECASE | re.MULTILINE)
_ANY_TAG_RE = re.compile(r"<\s*/?\s*(?:%s)(?:\s[^>]*)?>" % _ALT, re.IGNORECASE)
# A fence tag left MID-line with no partner (a cut-off echo): drop the tag and
# the rest of that line, keep what came before it.
_ORPHAN_TAIL_RE = re.compile(
    r"[ \t]*<\s*/?\s*(?:%s)(?:\s[^>\n]*)?>[^\n]*" % _ALT, re.IGNORECASE)


def has_control_fence(text: Optional[str]) -> bool:
    """True when *text* carries an opening or closing control-fence tag."""
    return bool(text) and _ANY_TAG_RE.search(text) is not None


def strip_control_fences(text: Optional[str]) -> str:
    """Remove every control-fence block and every orphan fence tag from *text*.

    An orphan tag that opens its line removes the line; one in mid-line
    removes itself and the rest of that line.

    Plain text comes back unchanged. Blank lines that a removed block leaves
    behind collapse to one.
    """
    if not text:
        return ""
    if not has_control_fence(text):
        return text
    out = _BLOCK_RE.sub("", text)
    out = _ORPHAN_LINE_RE.sub("", out)
    out = _ORPHAN_TAIL_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def strip_turn_fences(user_content: Optional[str],
                      assistant_content: Optional[str]) -> Tuple[str, Optional[str]]:
    """Both halves of a memory turn with their control fences stripped.

    The answer half is ``None`` when it held a fence and nothing else: the
    memory write path stores nothing for that turn.
    """
    user = strip_control_fences(user_content) if has_control_fence(user_content) \
        else (user_content or "")
    answer = assistant_content or ""
    if has_control_fence(answer):
        answer = strip_control_fences(answer)
        if not answer.strip():
            return user, None
    return user, answer
