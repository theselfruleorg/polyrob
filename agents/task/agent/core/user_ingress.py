"""User-message ingress + approval/TODO mixin (roadmap P9; code-motion from service.py).

The continuous-chat user-message intake, the (now auto-approving) approval shims,
and TODO-completion reporting — moved verbatim off the ``Agent`` god-file. ``Agent``
composes ``UserIngressMixin`` so call sites (orchestrator's
``agent.receive_user_message`` / ``agent.record_approval_decision``, and the run
loop's ``_drain_user_messages`` / ``_check_todo_completion``) are unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agents.task.telemetry.views import (
    HumanApprovalDecisionEvent,
    TodoStatusEvent,
)
from agents.task.agent.core.self_wake import FORGED_TURN_KINDS as _FORGED_MESSAGE_KINDS
from core.surfaces.inbound_attachments import ATTACHMENT_ONLY_KEY, TYPED_TEXT_KEY  # 085 W3

# Message kinds that count as the owner genuinely driving, and so clear correspondent
# taint. Mirrors hitl_ingress._TRUSTED_CONTEXT_REF_KINDS (the same allowlist that
# gates @-reference expansion); kept as a literal rather than an import to avoid an
# agents.agent.core -> agents.session edge for one frozenset.
_TAINT_CLEARING_KINDS = frozenset({"comment", "continuation"})

#: ``metadata["room_role"]`` stamped by the room steer (task_agent_delivery) on a
#: line that a room ADMIN, not the owner, wrote.
ROOM_ROLE_KEY = "room_role"


def _owner_spoke(message: Dict[str, Any]) -> bool:
    """False for a room admin's steer line; every other genuine kind is the owner's."""
    return str((message.get("metadata") or {}).get(ROOM_ROLE_KEY) or "") != "admin"


def typed_text_of(message: Dict[str, Any]) -> str:
    """The owner's typed words of one queued message, or ``""``: a forwarded body
    is a third party's words, and a message with no ``typed_text`` has none."""
    meta = message.get("metadata") or {}
    if meta.get("forwarded"):
        return ""
    typed = meta.get(TYPED_TEXT_KEY)
    return typed if isinstance(typed, str) else ""


def _update_forged_turn_marker(orchestrator, messages: List[Dict[str, Any]]) -> None:
    """SK-F10: recompute the forged-turn marker from a drained message batch.

    Recomputing at every drain (rather than set-on-forge / clear-at-one-fixed
    call site) means the marker can never leak past the point a genuine
    message actually arrives, on ANY path that queues a message (self-wake,
    async delegation, or a plain user continuation) — there is no separate
    "did we remember to clear it" step to forget on an exception path.

    - An empty batch changes nothing (mid-turn steps with no new messages
      stay whatever the turn already was).
    - Any forged-kind message in the batch marks the turn forged (fail
      toward untrusted on a mixed batch — see task-11 brief).
    - A batch with no forged-kind message (a genuine "comment"/"continuation"
      arrived) clears the marker — the owner is driving again.
    """
    if orchestrator is None or not messages:
        return
    forged_kind = next(
        (m.get("kind") for m in messages if m.get("kind") in _FORGED_MESSAGE_KINDS),
        None,
    )
    orchestrator._forged_turn_kind = forged_kind

    # AGT-4: the owner's OWN words for this turn — the only text a tool may take
    # as the owner's decision (owner_ask answer=). Recomputed per drained batch,
    # like the forged marker: a forwarded body is a third party's words; a room
    # admin's line is not the owner's; a forged message (self-wake, delegation
    # result) is never in it. 2026-10-13 review C10: a MIXED batch (the owner's
    # answer drained with a delegation result) keeps the owner's genuine lines —
    # it used to be discarded whole, so the owner's real answer was refused as a
    # decision. The turn stays marked forged above (fail toward untrusted).
    # 085 W3 (A1): only what the owner TYPED — ``metadata["typed_text"]``, stamped
    # at the queue (``hitl_ingress.submit_user_message``) before @-references expand
    # and by every surface that appends attachment or caption content to the text.
    # A message with no ``typed_text`` contributes nothing: the full ``text`` may hold
    # a document's content, which is never the owner's own words.
    # A batch of only attachment turns (CLI ``--attach``) keeps the words the owner
    # typed for this turn (the opening task, the REPL line): it has none of its own.
    owner_lines = [m for m in messages
                   if m.get("kind") in _TAINT_CLEARING_KINDS and _owner_spoke(m)]
    if not (owner_lines and all((m.get("metadata") or {}).get(ATTACHMENT_ONLY_KEY)
                                for m in owner_lines)):
        orchestrator._owner_turn_text = "\n".join(
            typed for typed in (typed_text_of(m) for m in owner_lines) if typed)
        # WS5 D4: the latest genuine owner ask, DATED — a delegated child is briefed
        # with it. Unlike ``_owner_turn_text`` it survives a later forged batch.
        if orchestrator._owner_turn_text and orchestrator._owner_turn_text.strip():
            import time as _time
            orchestrator._last_owner_ask = (orchestrator._owner_turn_text.strip(), _time.time())

    # WS-A: a genuine owner/continuation turn clears any correspondent taint — the
    # owner is driving again, so the capability gate re-opens high-impact tools.
    #
    # This lives HERE, at the drain, and not in submit_user_message where it used to.
    # Clearing on submit cleared it when the message was merely QUEUED, including on
    # the three paths that accept nothing: target agent not found (silent return),
    # pending-queue full, and HITL-queue full (both raise MessageQueueFullError). The
    # gate then re-opened on untrusted correspondent data with no owner turn ever
    # entering. Draining is where the message provably enters the turn, and it is the
    # same place — and the same "is the owner driving?" question — the forged-turn
    # marker above is recomputed from. Fail-open.
    # AGT-17: a room ADMIN's addressed line rides kind="comment" too, but an admin
    # is not the owner driving — it never clears the taint.
    if any(m.get("kind") in _TAINT_CLEARING_KINDS and _owner_spoke(m) for m in messages):
        # The owner is speaking again: the ONE clear point.
        from core.security.taints import clear_on_owner_turn
        clear_on_owner_turn(orchestrator)

    # P1 finalization: a genuine (non-forged) batch means the owner is driving
    # again — clear the self-wake re-entry budget for this session. Previously the
    # only ReentryBudget.reset() caller was dead code, so after SELF_WAKE_MAX_REENTRIES
    # forged wakes self-wake died PERMANENTLY for a session and never recovered from
    # real conversation (only a 7-day staleness purge cleared it). Fail-open.
    if forged_kind is None:
        try:
            from agents.task.agent.core.self_wake import get_reentry_budget
            sid = getattr(orchestrator, "session_id", "") or ""
            if sid:
                get_reentry_budget().reset(sid)
        except Exception:
            pass


#: 085 W3: explicit ``creator`` labels whose session task IS the line a person
#: typed at a seat (chat_once, the REPL / ``polyrob run``, the HTTP endpoint).
_TYPED_TASK_CREATORS = frozenset({"owner", "cli", "api"})


def opening_owner_text(kwargs: Dict[str, Any], task: Any, *, public: bool = False) -> str:
    """The owner's typed words for a new session's opening turn (085 W3, A1).

    ``typed_text`` passed by the surface wins (``""`` for a forwarded message).
    Otherwise only an EXPLICIT human-seat creator seeds the task; a goal or
    cron dispatch (no creator), a correspondent and a room seed ``""``."""
    if "typed_text" in kwargs:
        typed = kwargs.get("typed_text")
        return typed if isinstance(typed, str) else ""
    if public or kwargs.get("creator") not in _TYPED_TASK_CREATORS:
        return ""
    return task if isinstance(task, str) else ""


def latest_turn_text(messages: List[Dict[str, Any]]) -> Optional[str]:
    """B1: the newest GENUINE message text of a drained batch — the recall subject
    for this turn (``MemoryPrefetchMixin._build_recall_query``). None for an empty
    or forged batch, so a self-wake keeps the last real subject."""
    for m in reversed(messages or []):
        if m.get("kind") in _TAINT_CLEARING_KINDS:
            text = str(m.get("text") or "").strip()
            if text:
                return text
    return None


def _update_turn_reply_to(orchestrator, messages: List[Dict[str, Any]]) -> None:
    """044 T10 fix round 1: recompute the room reply-anchor from this drained
    batch, at the SAME per-drain seam the forged-turn marker (above) and the
    reply latch (``core.surfaces.turn_reply.reset_turn``) are recomputed at —
    never "set once, cleared somewhere else", so no path can leave a stale
    anchor behind.

    The anchor rides ON THE MESSAGE (``metadata['reply_to']``, stamped by the
    harness only onto a message that actually entered the queue — a rejected
    "queue full" submission never reaches here, so a busy-path race can never
    set a stale anchor). A plain STEER message with no anchor, a self-wake, or
    an async-delegation reentry (none of which carry ``reply_to``) therefore
    CLEARS any anchor left over from an earlier room trigger — closing both
    "persists indefinitely" and "an autonomous continuation reuses it".

    An empty batch changes nothing (mirrors ``_update_forged_turn_marker``:
    a mid-turn step with no new messages keeps whatever the turn already had).
    Fail-open: an unreadable orchestrator/messages shape just leaves the
    anchor as it was.
    """
    if orchestrator is None or not messages:
        return
    last_meta = messages[-1].get("metadata") or {}
    orchestrator._turn_reply_to = last_meta.get("reply_to")


def _stamp_delegation_deliveries(agent, messages: List[Dict[str, Any]]) -> None:
    """T1.6 review fix (CRITICAL finding): stamp ``delivered_at`` for any
    drained message that is an async-delegation completion or a cold-start
    completed-undelivered recovery.

    This is the ONE point a delegation result is guaranteed to be handed to
    ``inject_user_guidance()`` — by this call's caller, synchronously, right
    after drain — and so has ACTUALLY entered the turn's message history.
    Contrast with submit/park time (``orchestrator._deliver_async_delegation`` /
    ``TaskAgent.deliver_self_wake``), which only places the message in the
    in-memory HITL queue; a crash before this drain must leave the row
    recoverable, not falsely marked delivered.

    Fail-open throughout — never allowed to block message delivery.
    """
    if not messages:
        return
    session_id = getattr(agent, "session_id", "") or ""
    if not session_id:
        return
    user_id = getattr(agent, "user_id", "") or ""
    try:
        from agents.task.agent.autonomy_state import stamp_delivered_from_drain
    except Exception:
        return
    for msg in messages:
        try:
            stamp_delivered_from_drain(
                session_id, user_id, msg.get("kind", ""), msg.get("metadata"),
            )
        except Exception:
            pass


class UserIngressMixin:
    """User-message queueing, approval shims, and TODO status for Agent."""

    def set_turn_input(self, text: str) -> None:
        """Conversational turn input: append the user's message as an ordinary trailing
        turn (R4), distinct from inject_user_guidance's position-1 task-directive
        framing. (Folded in from the one-method ``TurnInputMixin``, W9.)"""
        self.message_manager.append_user_turn(text)

    async def receive_user_message(self, text: str, kind: str = "comment", metadata: Optional[dict] = None) -> None:
        """Queue user message for injection in next step."""
        if metadata is None:
            metadata = {}
        await self.hitl_manager.queue_user_message(text, kind, metadata)

    async def _drain_user_messages(self) -> List[Dict[str, Any]]:
        """Drain queued user messages and detect workspace changes.

        Returns:
            List of user message dicts. Also sets self._session_continuation_context
            if messages are present, for use by inject_user_guidance().
        """
        messages = await self.hitl_manager.drain_user_messages()

        # B1: the turn's own words drive memory recall, not the session's first task.
        _turn_text = latest_turn_text(messages)
        if _turn_text:
            self._recall_turn_text = _turn_text

        # SK-F10: recompute the forged-turn marker on the orchestrator from this
        # batch's kinds, fail-open (never let this break message delivery).
        try:
            _update_forged_turn_marker(getattr(self, 'orchestrator', None), messages)
        except Exception:
            pass

        # C1 (communication contract): a drained batch IS the start of a turn, so
        # clear the per-turn reply latch here — the same seam, and for the same
        # reason, as the forged-turn marker above: recomputed at the drain rather
        # than "set once and cleared somewhere else", so no exception path can
        # leave a stale latch that silences the next turn's `done`. Fail-open.
        if messages:
            try:
                from core.surfaces.turn_reply import reset_turn
                reset_turn(getattr(self, 'orchestrator', None))
            except Exception:
                pass

            # 044 T10 fix round 1: recompute the room reply-anchor from this
            # batch, fail-open (never let this break message delivery).
            try:
                _update_turn_reply_to(getattr(self, 'orchestrator', None), messages)
            except Exception:
                pass

        # T1.6 review fix: stamp delivered_at for any drained delegation-result /
        # completed-undelivered-recovery message. Must run here (the drain
        # point), not at submit/park time — see _stamp_delegation_deliveries.
        try:
            _stamp_delegation_deliveries(self, messages)
        except Exception:
            pass

        # 061: a GENUINE owner batch brings the thread with it — the referent of
        # a quote-reply, and (mid-run only; a turn's first step injects its own
        # delta) what other sessions/rails said meanwhile. Fail-open.
        if messages and getattr(getattr(self, 'orchestrator', None),
                                '_forged_turn_kind', None) is None:
            try:
                if hasattr(self, '_inject_owner_thread_referents'):
                    self._inject_owner_thread_referents(messages)
                if getattr(self.state, 'n_steps', 0) >= 1 and \
                        hasattr(self, '_inject_owner_thread_delta'):
                    self._inject_owner_thread_delta()
            except Exception:
                pass

        # Prepare session context for message injection
        if messages:
            # NEW: Get workspace changes
            workspace_changes = None
            try:
                workspace_changes = self.workspace_context.get_workspace_changes(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    since_last_check=True
                )

                if workspace_changes.has_changes():
                    self.logger.info(
                        f"Workspace changes detected: "
                        f"+{len(workspace_changes.added)} files, "
                        f"~{len(workspace_changes.modified)} modified"
                    )
            except Exception as e:
                self.logger.warning(f"Failed to get workspace changes: {e}")

            session_context = {
                'continuation': True,  # Signal this is a continuation (user sent new message)
                'workspace_changes': workspace_changes  # NEW: Include workspace context
            }

            # Record the harness note the model is about to see, so a reviewer
            # can tell what it was told (prod: "the file you uploaded" came from
            # this note, not from the owner). Fail-open.
            try:
                note = (workspace_changes.format_for_agent()
                        if workspace_changes is not None and workspace_changes.has_changes()
                        else "")
                if note and getattr(self, 'telemetry_manager', None):
                    from agents.task.telemetry.views import UserMessageDuringExecutionEvent
                    self.telemetry_manager.capture_event(UserMessageDuringExecutionEvent(
                        agent_id=self.agent_id,
                        step=getattr(self.state, 'n_steps', 0),
                        message_text=str(messages[0].get('text', ''))[:200],
                        message_kind=str(messages[0].get('kind', 'comment')),
                        queue_depth=len(messages),
                        execution_phase="drained",
                        harness_note=note[:1000],
                    ))
            except Exception:
                pass

            # Store for inject_user_guidance to use
            self._session_continuation_context = session_context

            self.logger.info(
                f"Drained {len(messages)} user messages. "
                f"Context: continuation=True, "
                f"workspace_changes={workspace_changes.has_changes() if workspace_changes else False}"
            )

        return messages

    async def record_approval_decision(self, approved: bool, note: Optional[str] = None,
                                       override_type: Optional[str] = None,
                                       edited_params: Optional[Dict[str, Any]] = None):
        """Record a human approval decision.

        Simplified implementation: Just injects user guidance without complex approval state.
        """
        # Emit telemetry
        event = HumanApprovalDecisionEvent(
            agent_id=self.agent_id,
            step=self.state.n_steps,
            approved=approved,
            note=note,
            decision_time_seconds=0,  # Not tracking decision time anymore
            override_type=override_type,
            edited_params=edited_params
        )
        self.telemetry_manager.capture_event(event)

        # Inject as user guidance (for both approval and rejection)
        if note:
            kind = 'comment' if approved else 'correction'
            self.message_manager.inject_user_guidance([{
                'text': f"{'Approved' if approved else 'Rejected'}: {note}",
                'kind': kind,
                'metadata': {'source': 'approval_decision'}
            }])

    def _check_todo_completion(self) -> bool:
        """Check if all TODOs are completed using TaskTool.

        Returns:
            True if all TODOs are complete or no TaskTool available
        """
        try:
            # Get TaskTool directly from controller
            task_tool = self.controller.get_tool('task')
            if not task_tool:
                return True  # No TaskTool, assume complete

            # Check completion using TaskTool
            todos_complete = task_tool.check_completion(self.session_id)

            # Log status
            if hasattr(task_tool, 'get_progress'):
                progress = task_tool.get_progress(self.session_id)
                self.logger.info(f"TODO Status: {progress.get('completed', 0)}/{progress.get('total', 0)} complete")

            return todos_complete
        except Exception as e:
            self.logger.debug(f"Error checking TODO status: {e}")
            # Fail-safe: assume complete on error (Controller will provide soft warning if needed)
            return True

    def _emit_todo_status(self, enforcement_triggered: bool = False):
        """Emit a TODO status telemetry event using TaskTool.

        Args:
            enforcement_triggered: Whether TODO enforcement blocked completion
        """
        try:
            # Get TaskTool directly from controller
            task_tool = self.controller.get_tool('task')
            if not task_tool:
                return

            # Get progress and tasks from TaskTool
            progress = task_tool.get_progress(self.session_id)
            todos = task_tool.get_all_tasks(self.session_id)

            # Emit telemetry event
            event = TodoStatusEvent(
                agent_id=self.agent_id,
                todos_total=progress.get('total', 0),
                todos_completed=progress.get('completed', 0),
                todos_pending=progress.get('pending', 0),
                todo_items=todos,
                enforcement_triggered=enforcement_triggered
            )
            self.telemetry_manager.capture_event(event)
        except Exception as e:
            self.logger.debug(f"Error emitting TODO status: {e}")
