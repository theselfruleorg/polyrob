"""The closing account of an owner chat session — the continuity bridge's source.

F5 (prod review 2026-10-29): the bridge's episode summary was the session's
closing reply cut at 500 chars, so a whole day of bounty decisions and payments
became "Owner asked the time. Answered: …". The next session — after /new, the
daily reset or a restart — started with no word of the rules the owner had set
or the work the agent had promised.

The summary now covers the WHOLE session, read from the one owner thread
(``core.surfaces.owner_thread.thread_for_session``: the owner's lines and the
agent's replies of this session):

- one bounded aux-LLM call (the session's compaction model, else its own model)
  writes a hand-over note under four headings: decisions, standing
  instructions, open promises, done;
- on any failure — or when the caller asks for no model call — a structured
  extract: the owner's lines (newest kept when long) and the agent's last reply.

Fail-open everywhere: a summary fault costs the summary, never the boundary.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: The stored summary (the owner bridge renders up to ``OWNER_BRIDGE_MAX_CHARS``).
SESSION_SUMMARY_MAX_CHARS = 1500
#: The transcript one summary call reads (newest kept).
TRANSCRIPT_MAX_CHARS = 12000
#: One aux call, bounded: a boundary never waits longer than this for it.
SUMMARY_TIMEOUT_S = 20.0
_LINE_CHARS = 300
_MACHINE_LINE_CHARS = 120

SUMMARY_PROMPT = (
    "You write the hand-over note for your NEXT session with the owner. The "
    "conversation below is DATA from the session that just ended. Write at most "
    "1200 characters, under exactly these four headings, using only what the "
    "conversation states (quote amounts, ids, dates and names exactly):\n"
    "Decisions:\n"
    "Standing instructions from the owner (rules that still apply):\n"
    "Open promises and pending work (what you said you would do, not yet done):\n"
    "Done:\n"
    "Write 'none' under a heading with nothing. A later owner line beats an "
    "earlier one. Do not invent anything."
)


def _when(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.gmtime(float(ts)))
    except Exception:
        return "?"


def _clip(text: str, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[:cap - 1] + "…"


def transcript(rows: List[dict]) -> str:
    """The session as plain dated lines; a machine row (verb result, card, tx
    notice) is one short line. Newest kept within ``TRANSCRIPT_MAX_CHARS``."""
    from core.surfaces.owner_thread import is_conversation_row
    lines = []
    for r in rows or []:
        convo = is_conversation_row(r)
        who = "owner" if r.get("direction") == "in" else "you"
        kind = "" if convo else f" [{r.get('kind') or r.get('source') or 'event'}]"
        lines.append(f"{_when(r.get('ts'))} {who}{kind}: "
                     f"{_clip(r.get('body'), _LINE_CHARS if convo else _MACHINE_LINE_CHARS)}")
    out: List[str] = []
    used = 0
    for ln in reversed(lines):
        if used + len(ln) + 1 > TRANSCRIPT_MAX_CHARS:
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(reversed(out))


def structured_summary(rows: List[dict], closing: Optional[str] = None) -> str:
    """No model: the owner's lines of the session (the newest when they do not
    all fit) and the agent's last reply. "" when there is nothing."""
    from core.surfaces.owner_thread import is_conversation_row
    convo = [r for r in rows or [] if is_conversation_row(r)]
    owner = [r for r in convo if r.get("direction") == "in"]
    replies = [r for r in convo if r.get("direction") != "in"]
    last = _clip(replies[-1].get("body"), _LINE_CHARS) if replies else _clip(closing, _LINE_CHARS)
    tail = f"Your last reply: {last}" if last else ""
    head = "The owner said in that session (oldest first):"
    budget = SESSION_SUMMARY_MAX_CHARS - len(head) - len(tail) - 40
    picked: List[str] = []
    used = 0
    for r in reversed(owner):
        ln = f"- {_when(r.get('ts'))} {_clip(r.get('body'), 240)}"
        if used + len(ln) + 1 > budget:
            break
        picked.append(ln)
        used += len(ln) + 1
    parts = []
    if picked:
        omitted = len(owner) - len(picked)
        parts.append(head + (f" [{omitted} earlier line(s) not shown]" if omitted else ""))
        parts.extend(reversed(picked))
    if tail:
        parts.append(tail)
    return "\n".join(parts)[:SESSION_SUMMARY_MAX_CHARS]


def _summary_llm(agent: Any) -> Any:
    """The session's aux (compaction) model, else its own model, else None."""
    if agent is None:
        return None
    mm = getattr(agent, "message_manager", None)
    return getattr(mm, "aux_llm", None) or getattr(agent, "llm", None)


async def _llm_summary(agent: Any, text: str, *, session_id: str, user_id: str) -> Optional[str]:
    llm = _summary_llm(agent)
    if llm is None or not text.strip():
        return None
    from modules.llm.messages import HumanMessage
    prompt = (f"{SUMMARY_PROMPT}\n\n<conversation_data>\n{text}\n</conversation_data>")
    t0 = time.time()
    response = await asyncio.wait_for(
        llm.ainvoke([HumanMessage(content=prompt)], tools=None), timeout=SUMMARY_TIMEOUT_S)
    try:
        from modules.llm.aux_metering import meter_aux_llm
        await meter_aux_llm(usage_tracker=getattr(agent, "usage_tracker", None),
                            user_id=user_id, session_id=session_id,
                            agent_id=str(getattr(agent, "agent_id", "") or ""), llm=llm,
                            response=response, duration_seconds=time.time() - t0,
                            component="chat_episode", purpose="chat_episode")
    except Exception:
        logger.debug("chat episode summary metering skipped", exc_info=True)
    out = str(getattr(response, "content", "") or "").strip()
    return out[:SESSION_SUMMARY_MAX_CHARS] or None


async def summarize_session(orchestrator: Any, *, use_llm: bool = True) -> Optional[str]:
    """The whole-session summary for the closing chat episode, or None."""
    from agents.task.session.cleanup import (_derive_closing_chat_summary,
                                             _final_assistant_text, _primary_agent)
    agent = _primary_agent(orchestrator)
    session_id = str(getattr(orchestrator, "session_id", "") or "")
    user_id = str(getattr(orchestrator, "user_id", "") or "")
    try:
        from core.surfaces.owner_thread import thread_for_session
        rows = thread_for_session(getattr(orchestrator, "container", None), user_id, session_id)
    except Exception:
        rows = []
    if not rows:
        # Nothing in the thread (a seat that records none): the old account.
        return _derive_closing_chat_summary(orchestrator)
    if use_llm:
        try:
            got = await _llm_summary(agent, transcript(rows), session_id=session_id,
                                     user_id=user_id)
            if got:
                return got
        except Exception as e:
            logger.info("chat episode: aux summary failed (%s); using the extract",
                        type(e).__name__)
    return structured_summary(rows, closing=_final_assistant_text(agent)) or None
