"""061 — the session READS the owner thread; it does not own it.

Mixin composed into ``Agent`` beside ``MemoryPrefetchMixin``. Three readers,
one renderer (``core.surfaces.owner_thread.render_block``):

- **tail** — the first step of an owner session (R1 Telegram DM / console /
  REPL, R2 task session): the last hours/rows of the ONE conversation, every
  session and rail. Durable in history (``push_control_message``) because the
  turn that answers "explain better" reads it on step 3, not only on step 1.
- **delta** — the first step of every LATER turn, and any batch drained
  mid-run: what OTHER sessions and rails exchanged with the owner since this
  session's last owner turn. Empty delta = no message.
- **referent** — a quote-reply: the exact line the owner answered, resolved
  from the surface message id.
- **rail slice** (R4) — an autonomous run's first step: what THIS rail last
  told the owner and what the owner replied to it.

Never: a room (public) session, a sub-agent, a correspondent-facing session
(created for a correspondent or correspondent-tainted — H16). Fail-open: a missing store or a
render fault costs the block, never the turn. Gated ``OWNER_THREAD_INJECT``.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

_SEEN_ATTR = "_owner_thread_seen_ts"
_REPLY_ATTR = "_owner_thread_reply_to"


class OwnerThreadInjectMixin:

    # --- guards ---------------------------------------------------------------
    def _owner_thread_applicable(self) -> bool:
        try:
            from core.surfaces.owner_thread import owner_thread_inject
            if not owner_thread_inject():
                return False
            if getattr(self, "_is_sub_agent", False):
                return False
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                return False
            # H16: never into a session a correspondent drives — created for one
            # (conversation resume) or tainted by one. Its output goes to them.
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return False
            return bool(str(getattr(self, "user_id", "") or "").strip())
        except Exception:
            return False

    def _owner_thread_push(self, block: str) -> None:
        if not block:
            return
        from modules.llm.messages import MessageOrigin, make_control_message
        msg = make_control_message(block, MessageOrigin.OWNER_THREAD)
        mm = getattr(self, "message_manager", None)
        if mm is not None and hasattr(mm, "push_control_message"):
            mm.push_control_message(msg)
        elif mm is not None and hasattr(mm, "push_ephemeral_message"):
            mm.push_ephemeral_message(msg)

    def _owner_thread_mark_seen(self, ts: Optional[float] = None) -> None:
        orch = getattr(self, "orchestrator", None)
        if orch is not None:
            setattr(orch, _SEEN_ATTR, float(ts if ts is not None else time.time()))

    def _owner_thread_seen_ts(self) -> Optional[float]:
        """This session's watermark: in-process, else the store's own record of
        the session's newest owner line (so a session recreated from disk
        computes the same delta), else None (a fresh session: the tail applies)."""
        orch = getattr(self, "orchestrator", None)
        ts = getattr(orch, _SEEN_ATTR, None) if orch is not None else None
        if ts:
            return float(ts)
        from core.surfaces.owner_thread import last_owner_turn_ts
        return last_owner_turn_ts(getattr(self, "container", None),
                                  getattr(self, "user_id", ""), self.session_id)

    # --- readers --------------------------------------------------------------
    async def _maybe_inject_owner_thread(self) -> None:
        """First step of a turn: tail (session bootstrap) or delta (later turn);
        rail slice for an autonomous run; the referent of a fresh quote-reply."""
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if not self._owner_thread_applicable():
                return
            from core.surfaces.owner_thread import (
                rail_slice, referent, render_block, thread_tail)
            container = getattr(self, "container", None)
            user_id = getattr(self, "user_id", "")
            from agents.task.goals.autonomy_marker import (
                cron_job_for_session, goal_for_session, is_autonomous)
            bootstrapped = getattr(self, "_session_bootstrap_done", False)
            if is_autonomous(self.session_id):
                if bootstrapped:
                    return
                job = cron_job_for_session(self.session_id)
                goal = goal_for_session(self.session_id)
                rail_id = f"cron:{job}" if job else (f"goal:{goal}" if goal else "")
                rows = rail_slice(container, user_id, rail_id=rail_id)
                self._owner_thread_push(render_block(rows, kind="rail"))
                return
            if not bootstrapped:
                rows = thread_tail(container, user_id)
                self._owner_thread_push(render_block(rows, kind="tail",
                                                     this_session=self.session_id))
                self._owner_thread_mark_seen()
            else:
                self._inject_owner_thread_delta()
            orch = getattr(self, "orchestrator", None)
            ref = getattr(orch, _REPLY_ATTR, None) if orch is not None else None
            if ref:
                try:
                    setattr(orch, _REPLY_ATTR, None)
                    row = referent(container, user_id, via=ref[0], mid=ref[1])
                    if row:
                        self._owner_thread_push(render_block([row], kind="referent"))
                except Exception:
                    pass
        except Exception as e:
            self.logger.debug(f"owner thread injection skipped: {e}")

    def _inject_owner_thread_delta(self) -> None:
        """Rows OTHER sessions/rails produced since this session's watermark."""
        try:
            if not self._owner_thread_applicable():
                return
            from agents.task.goals.autonomy_marker import is_autonomous
            if is_autonomous(self.session_id):
                return
            since = self._owner_thread_seen_ts()
            if since is None:
                return
            from core.surfaces.owner_thread import render_block, thread_delta
            rows = thread_delta(getattr(self, "container", None),
                                getattr(self, "user_id", ""),
                                session_id=self.session_id, since_ts=since)
            self._owner_thread_mark_seen()
            if rows:
                self._owner_thread_push(render_block(rows, kind="delta",
                                                     this_session=self.session_id))
        except Exception as e:
            self.logger.debug(f"owner thread delta skipped: {e}")

    def _inject_owner_thread_referents(self, messages: List[dict]) -> None:
        """A drained STEER batch: render the referent of each quote-reply."""
        try:
            refs = [(m.get("metadata") or {}).get("owner_thread_reply_to")
                    for m in (messages or []) if isinstance(m, dict)]
            refs = [r for r in refs if r]
            if not refs or not self._owner_thread_applicable():
                return
            from core.surfaces.owner_thread import referent, render_block
            container = getattr(self, "container", None)
            user_id = getattr(self, "user_id", "")
            for via, mid in refs[:3]:
                row = referent(container, user_id, via=str(via), mid=str(mid))
                if row:
                    self._owner_thread_push(render_block([row], kind="referent"))
        except Exception as e:
            self.logger.debug(f"owner thread referent skipped: {e}")
