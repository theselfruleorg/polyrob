"""044 T20: a cron job (or goal) that SERVICES a room.

The run session BINDS to the room — same ``session_source`` and
``chat_session_key``, so ``bind_chat_surface`` stamps ``_public_session`` and the
PUBLIC profile + room tool gate apply exactly as they do for a live room turn,
and every reply routes into the room.

⚠️ It binds but does NOT take over. The callers pass ``bind_write_row=False``, so
the durable chat<->session row keeps naming the room's LIVE session (fix round 1,
Critical 1): a service tick that re-pointed the room key at its own one-shot
session would make the next human line land inside the service rubric — an owner
answered with ``[SILENT]`` — orphan the real room session, and refresh
``updated_at`` so the idle reset never fired.

Three pieces, one per phase of a run:

- :func:`room_binding` — ``payload.group`` -> ``(SessionSource, session_key)``.
- :func:`build_service_task` — ``(turn_text, None)``, or ``(None, skip_reason)``
  when the room's own policy or an empty tail says this tick must cost nothing.
- :func:`finish_service_run` — mark what the run answered, advance the checkpoint.

⚠️ The checkpoint (``reader="goal"``) is what bounds CATCH-UP; ``answered_by`` is
stamped at LIVE-turn dispatch (T14) and is a second, narrower filter. Both are
applied: a line the room turn already handled is not service work, and a line
older than the checkpoint is not re-read even if nothing ever marked it.

Layering: this module is in the agents tier, so it may import core. It imports
nothing from tools/surfaces.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional, Tuple

from core.surfaces.envelopes import SessionSource
from core.surfaces.session_chat_registry import build_session_key

logger = logging.getLogger(__name__)

#: How many ledger lines one service run reads at most. The block renderer caps
#: the rendered SIZE (``CONTEXT_BLOCK_MAX_CHARS``) and says how many it dropped,
#: so this only bounds the query.
_TAIL_LIMIT = 100

#: Fix round 1 (Important 4): how far back a run may reach when it has NO
#: checkpoint yet. Without this a freshly scheduled job's first tick pulls the
#: whole retention window (``GROUP_LEDGER_RETENTION_DAYS``, 14 days) and answers
#: a fortnight of stale chatter as if it had just been said.
_FIRST_RUN_LOOKBACK_SEC = 24 * 3600

# --- typed skip reasons ------------------------------------------------------
# A run that must cost nothing says WHY. Cron emits `skipped/<reason>`; the goal
# path logs it. "The room went quiet" and "the owner muted the room" are not the
# same fact and must not read the same in the event log.
SKIP_MODE_OFF = "mode_off"
SKIP_LISTEN = "listen"
SKIP_MUTED = "muted"
SKIP_QUIET_HOURS = "quiet_hours"
SKIP_NO_CHANGE = "no_change"

_RUBRIC = (
    "You are servicing the room below. Read the lines. Reply ONLY to lines that ask "
    "something you can answer, that address you, or that the standing instructions "
    "tell you to handle. Ignore chatter. At most {n} {reply_noun} this run; answer "
    "each chosen line with send_message(reply_to=<its message id>). If nothing needs "
    "you, reply exactly [SILENT]."
)


def _rubric(n: int) -> str:
    """The run's instruction, with the noun agreeing (fix round 2, Obs 4): a cap
    of one read "At most 1 replies", which is exactly the sloppiness a model
    imitates."""
    return _RUBRIC.format(n=n, reply_noun="reply" if n == 1 else "replies")


def room_binding(payload: Any) -> Optional[Tuple[SessionSource, str]]:
    """``(SessionSource, session_key)`` for a ``payload.group`` run, else None.

    ``chat_type`` defaults to ``supergroup`` — the key's chat_type segment is what
    makes ``is_group_session_key`` (and therefore the room caps, the ``[SILENT]``
    rule and the secret re-scrub in ``MessageRouter.publish``) recognise the
    target as a ROOM. Defaulting it to ``dm`` would route a room reply through
    the private-chat rails.
    """
    group = (payload or {}).get("group") if isinstance(payload, dict) else None
    if not isinstance(group, dict):
        return None
    surface = str(group.get("surface") or "").strip()
    chat_id = group.get("chat_id")
    if not surface or chat_id is None or not str(chat_id).strip():
        return None
    thread = group.get("thread_id")
    src = SessionSource(
        surface_id=surface,
        chat_id=str(chat_id),
        chat_type=str(group.get("chat_type") or "supergroup"),
        thread_id=(str(thread) if thread not in (None, "") else None),
    )
    return src, build_session_key(src)


def _ledger(container: Any):
    """The registered ``group_ledger``, else a handle on an EXISTING
    ``surfaces.db``. Never CREATES the store: a service run that cannot find a
    room log has nothing to service, and a read must not manufacture a db in
    whatever data home the process happens to resolve."""
    if container is None:
        return None
    try:
        svc = container.get_service("group_ledger")
    except Exception as e:
        logger.debug("group service: get_service(group_ledger) failed: %s", e)
        svc = None
    if svc is not None:
        return svc
    try:
        path = os.path.join(_data_home(container), "surfaces.db")
        if not os.path.exists(path):
            return None
        from core.surfaces.group_ledger import GroupLedger
        return GroupLedger(path)
    except Exception as e:
        logger.warning("group service: ledger unavailable (%s)", e)
        return None


def _data_home(container: Any) -> str:
    from core.runtime_paths import container_data_home
    return container_data_home(container)


def _policy_skip(policy: Any, now: float) -> Optional[str]:
    """Why this room's own policy says the tick must cost nothing, or None.

    Fix round 1 (Important 2): ``chat.mode``, ``chat.mute_until`` and
    ``chat.quiet_hours`` bound the LIVE turn (``chat_policy.mode_allows_trigger``)
    but bounded nothing here — a muted room, a room in quiet hours, even a room
    set to ``off`` was still being answered on a cadence. The service run is
    unaddressed by construction, so ``listen`` (addressed owner/admin only) is a
    skip too: there is nobody to be addressed BY.
    """
    if policy is None:
        return None
    mode = str(getattr(policy, "mode", "mention") or "mention")
    if mode == "off":
        return SKIP_MODE_OFF
    if mode == "listen":
        return SKIP_LISTEN
    try:
        if float(getattr(policy, "mute_until", 0) or 0) > now:
            return SKIP_MUTED
    except (TypeError, ValueError):
        pass
    from core.surfaces.chat_policy import _in_quiet_hours
    if _in_quiet_hours(getattr(policy, "quiet_hours", "") or "", now):
        return SKIP_QUIET_HOURS
    return None


def _load_policy(container: Any, owner_uid: str, src: SessionSource):
    try:
        from core.surfaces.chat_policy import load as load_policy
        return load_policy(_data_home(container), owner_uid, src.surface_id, src.chat_id)
    except Exception as e:  # fail-open: a policy fault costs the name, not the run
        logger.debug("group service: chat policy unreadable (%s)", e)
        return None


def _effective_max_replies(payload: Any, ceiling: int) -> int:
    """The room's own ``max``, clamped by the operator ceiling.

    Fix round 1 (Important 3): ``/groups service here max 1`` wrote
    ``payload.max_replies`` and SAID it was honoured, while both callers passed
    the env value straight through — so the per-room number never reached the
    rubric. A room may only narrow the ceiling, never widen it.
    """
    ceiling = max(1, int(ceiling))
    raw = (payload or {}).get("max_replies") if isinstance(payload, dict) else None
    try:
        if raw is not None:
            return max(1, min(int(raw), ceiling))
    except (TypeError, ValueError):
        logger.debug("group service: unusable payload.max_replies %r", raw)
    return ceiling


def build_service_task(container: Any, payload: Any, *, owner_uid: str,
                       max_replies: int,
                       now: Optional[float] = None
                       ) -> Tuple[Optional[str], Optional[str]]:
    """``(turn_text, None)`` for a run that has work, else ``(None, reason)``.

    NOTE: the text becomes the run session's durable TASK. A cron/goal run
    session is one-shot (created, run, released), so storing the rendered room
    tail on it is bounded by that session's own lifetime — it is not pinned into
    a long-lived chat session's history.
    """
    bound = room_binding(payload)
    if bound is None:
        return None, None
    src, _key = bound
    now = time.time() if now is None else now
    policy = _load_policy(container, owner_uid, src)
    skip = _policy_skip(policy, now)
    if skip is not None:
        return None, skip

    ledger = _ledger(container)
    if ledger is None:
        return None, SKIP_NO_CHANGE
    # Fix round 1 (Important 4): a job with no checkpoint yet must not reach back
    # over the whole retention window.
    since = max(ledger.checkpoint(src.surface_id, src.chat_id, "goal"),
                now - _FIRST_RUN_LOOKBACK_SEC)
    rows = ledger.tail(src.surface_id, src.chat_id, thread_id=src.thread_id,
                       since_ts=since, limit=_TAIL_LIMIT, unanswered_only=True)
    # 2026-09-16: the tail now carries the agent's OWN replies too (the room log
    # had only ever held the human half). Two different questions over one tail:
    #   * is there WORK? — only a human line can ask for an answer, so a tail of
    #     nothing but our own replies is still SKIP_NO_CHANGE;
    #   * what is the CONTEXT? — everything, ours included, or the run answers
    #     blind to what it already said and repeats itself at a stranger.
    if not any(not r.sender_is_bot for r in rows):
        return None, SKIP_NO_CHANGE

    from core.surfaces.group_turn import (frame_context, render_context,
                                          select_context_rows)
    # ONE selection decides what the model SEES and what it may answer. The id
    # list used to come from every row, so a tail over the 6000-char block cap
    # offered the model ids for lines it was never shown — it would then answer
    # from nothing. `select_context_rows` is pure, so this agrees exactly with
    # the block `render_context` renders (which keeps the `[N earlier lines
    # omitted]` note; dropping the OLDEST lines is accepted loss).
    kept, _omitted = select_context_rows(rows, exclude_message_id=None)
    # ⚠️ The offered id list is HUMAN-ONLY. `kept` is what the model was SHOWN
    # (so selection and rendering still agree on exactly that, which is the
    # hazard the note above guards), but handing it our own message ids would
    # invite the run to "reply to" a line it wrote itself.
    answerable = [r for r in kept if not r.sender_is_bot]
    if not answerable:
        return None, SKIP_NO_CHANGE
    chat_name = (getattr(policy, "name", "") or src.chat_id)
    ctx = render_context(rows, chat_name=chat_name, surface=src.surface_id,
                         thread=src.thread_id, exclude_message_id=None)
    n = _effective_max_replies(payload, max_replies)
    # 044 I4: the LIVE room turn frames its context block
    # (`push_room_context` -> `frame_context` -> `<untrusted_tool_result
    # source="group-context">`), and the SERVICE run did not — the same strangers'
    # lines arrived as the plain text of a task string, i.e. as instructions. It is
    # the more exposed of the two paths: nobody is watching a cron tick. ONE framing
    # rule, both paths.
    lines = [frame_context(ctx), "", _rubric(n)]
    instructions = getattr(policy, "instructions", "") or ""
    if instructions:
        lines += ["", f"Standing instructions for this room: {instructions}"]
    lines += ["", "Message ids, oldest first: "
              + ", ".join(str(r.message_id) for r in answerable)]
    return "\n".join(lines), None


def service_read_mark() -> float:
    """The wall clock a run READ at — pass it back as ``finish_service_run``'s
    ``up_to_ts`` so the checkpoint never jumps over a line that arrived while the
    run was still going (that line was never shown, and advancing past it would
    drop it forever)."""
    return time.time()


def finish_service_run(container: Any, payload: Any, *, session_id: str,
                       answered_ids: List[str],
                       up_to_ts: Optional[float] = None) -> None:
    """Mark what the run answered and advance its checkpoint.

    The checkpoint advances whatever the run's outcome was: presented IS handled
    (the same rule ``group_turn.mark_room_lines_answered`` applies at live
    dispatch), because a crashed, refused or TIMED-OUT run must not leave the
    room re-asking the same lines on every tick. Fail-open — this is
    bookkeeping, never a gate.
    """
    bound = room_binding(payload)
    ledger = _ledger(container)
    if bound is None or ledger is None:
        return
    src, _key = bound
    try:
        ids = [str(m) for m in (answered_ids or []) if str(m or "")]
        if ids:
            ledger.mark_answered(src.surface_id, src.chat_id, ids, session_id)
        rows = ledger.tail(src.surface_id, src.chat_id, thread_id=src.thread_id,
                           limit=_TAIL_LIMIT)
        if not rows:
            return
        newest = max(float(r.ts) for r in rows)
        if up_to_ts is not None:
            candidates = [float(r.ts) for r in rows if float(r.ts) <= float(up_to_ts)]
            newest = max(candidates) if candidates else 0.0
        if newest > 0:
            ledger.advance(src.surface_id, src.chat_id, "goal", newest)
    except Exception as e:
        logger.warning("group service: checkpoint bookkeeping failed for %s:%s: %s",
                       src.surface_id, src.chat_id, e)


def room_replied_ids(orchestrator: Any) -> List[str]:
    """The ledger ids this run actually REPLIED to.

    Recorded on the orchestrator by the outbound mirror whenever a room key is
    DELIVERED with a ``reply_to`` — i.e. by ``send_message(reply_to=…)``, the one
    verb the rubric tells the agent to answer with. Fail-open to ``[]``.
    """
    try:
        return [str(m) for m in (getattr(orchestrator, "_room_replied_ids", None) or [])
                if str(m or "")]
    except Exception:
        return []


def close_room_books(task_agent: Any, payload: Any, *, session_id: Optional[str],
                     up_to_ts: Optional[float]) -> None:
    """The ONE `finally`-safe wrapper both callers use (fix round 1, Important 5).

    A service run that is cancelled by its wall-clock timeout never returns a
    RunOutcome, so the old post-run call never ran and the next tick re-answered
    every line the cancelled one had already answered. The orchestrator is still
    resident at that moment, so its ``_room_replied_ids`` are read here; if it is
    gone the checkpoint STILL advances (presented is handled).
    """
    try:
        orch = None
        if session_id:
            try:
                orch = task_agent.get_orchestrator(session_id)
            except Exception:
                orch = None
        finish_service_run(getattr(task_agent, "container", None), payload,
                           session_id=session_id or "",
                           answered_ids=room_replied_ids(orch),
                           up_to_ts=up_to_ts)
    except Exception:
        logger.debug("group service: closing the room books failed", exc_info=True)


__all__ = ["SKIP_LISTEN", "SKIP_MODE_OFF", "SKIP_MUTED", "SKIP_NO_CHANGE",
           "SKIP_QUIET_HOURS", "build_service_task", "close_room_books",
           "finish_service_run", "room_binding", "room_replied_ids",
           "service_read_mark"]
