"""h_context.py — the ``/context`` REPL slash-command handler (owner-UX P1 T9).

Renders a transparency breakdown of what is actually assembled into the LLM
context window for the running session: one line per POPULATED foundation
slot (system prompt, runtime identity, SELF_CONTEXT, PROJECT_CONTEXT, skills,
initial task, conversation history) with its token count and % of context,
plus a total + context-limit footer.

``render_context_breakdown(message_manager) -> str`` is a PURE formatter over
the ``MessageManager``'s existing per-slot token accounting
(``agents/task/agent/messages/token_counter.py`` / the ``_*_tokens`` counters
set alongside each foundation message in ``agents/task/agent/message_manager/
service.py``) — no I/O, no LLM call, no H-MEM lookup (that would require a
``task_context_manager`` round-trip, which is not "pure over the manager").
Every accessor is read via ``getattr``/a local ``try/except`` so a manager
stub missing a slot (a future manager variant, a test double) degrades to
"slot omitted" rather than crashing the REPL.

Percentages are against the context limit (``max_input_tokens``) when known
(> 0), else against the observed total (so a stub manager with no limit still
renders a sane 100%-of-observed breakdown instead of dividing by zero).

Registration mirrors the ``/clear``/``/compact`` seam in ``handlers.py``:
``ctx.message_manager`` (``CommandContext.message_manager`` -> the live
session's ``agent.message_manager``) resolves the LIVE manager; no session/
manager yet -> the same friendly "no active session" line this module returns
for a ``None`` input, so the handler and the pure function agree.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from cli.ui import candy

_NO_SESSION_LINE = "No active session — start a chat, then run /context."


def _int_attr(obj: Any, name: str, default: int = 0) -> int:
    """Best-effort ``int(getattr(obj, name))``; never raises."""
    try:
        value = getattr(obj, name, default)
        return int(value) if value else 0
    except Exception:
        return default


# Foundation slots in assembly order (see ``messages/retrieval.py``
# ``get_messages_for_llm``): system -> runtime identity -> self_context ->
# project_context -> initial task -> skills -> (H-MEM, not pure — omitted).
_SLOT_SPECS: Tuple[Tuple[str, str], ...] = (
    ("system prompt", "_system_message_tokens"),
    ("runtime identity", "_runtime_identity_tokens"),
    ("self_context (SOUL/SELF)", "_self_context_tokens"),
    ("project_context", "_project_context_tokens"),
    ("initial task", "_initial_task_tokens"),
    ("skills", "_skill_message_tokens"),
)


def render_context_breakdown(message_manager: Any) -> str:
    """Render one line per populated context slot + a total/limit footer.

    Pure function over ``message_manager``: reads only its existing token
    counters, never mutates it, performs no I/O and no LLM/H-MEM calls.
    ``message_manager=None`` (no live session yet) returns the same friendly
    line the registered ``/context`` handler shows.
    """
    if message_manager is None:
        return _NO_SESSION_LINE

    slots: List[Tuple[str, int]] = []
    for label, attr in _SLOT_SPECS:
        tokens = _int_attr(message_manager, attr)
        if tokens > 0:
            slots.append((label, tokens))

    history_tokens = 0
    history = getattr(message_manager, "history", None)
    if history is not None:
        history_tokens = _int_attr(history, "total_tokens")
    if history_tokens > 0:
        slots.append(("history", history_tokens))

    total = sum(tokens for _, tokens in slots)
    limit = _int_attr(message_manager, "max_input_tokens")
    denom = limit if limit > 0 else total

    lines = ["context assembly (this session):"]
    if not slots:
        lines.append("  (no foundation slots populated yet)")

    total_pct = (total / denom * 100) if denom else 0.0
    rows = []
    for label, tokens in slots:
        pct = (tokens / denom * 100) if denom else 0.0
        rows.append((label, f"{tokens:,} tokens · {pct:.1f}%"))
    rows.append(("total", f"{total:,} tokens · {total_pct:.1f}%"))
    lines.append(candy.kv_lines(rows))

    if limit > 0:
        lines.append(f"context limit: {limit:,} tokens ({total_pct:.1f}% used)")
    else:
        lines.append("context limit: unknown (showing % of observed total)")

    return "\n".join(lines)
