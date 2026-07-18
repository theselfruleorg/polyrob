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
    HumanApprovalRequestedEvent,
    HumanApprovalDecisionEvent,
    TodoStatusEvent,
)
from agents.task.agent.core.self_wake import FORGED_TURN_KINDS as _FORGED_MESSAGE_KINDS


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


class UserIngressMixin:
    """User-message queueing, approval shims, and TODO status for Agent."""

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

        # SK-F10: recompute the forged-turn marker on the orchestrator from this
        # batch's kinds, fail-open (never let this break message delivery).
        try:
            _update_forged_turn_marker(getattr(self, 'orchestrator', None), messages)
        except Exception:
            pass

        # Prepare session context for message injection
        if messages:
            # Get current task phase for multi-phase tracking
            task_phase = 1
            if hasattr(self, 'session_manager') and self.session_manager:
                try:
                    task_phase = self.session_manager.get_task_phase(self.session_id)
                    # If phase is 0 (not set), keep at 1 for first continuation
                    if task_phase == 0:
                        task_phase = 1
                except Exception as e:
                    self.logger.debug(f"Could not get task_phase: {e}")

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
                'task_phase': task_phase,  # Include phase for phase-aware messaging
                'workspace_changes': workspace_changes  # NEW: Include workspace context
            }

            # Store for inject_user_guidance to use
            self._session_continuation_context = session_context

            self.logger.info(
                f"Drained {len(messages)} user messages. "
                f"Context: continuation=True, task_phase={task_phase}, "
                f"workspace_changes={workspace_changes.has_changes() if workspace_changes else False}"
            )

        return messages

    async def request_approval(self, reason: str, checkpoint_type: str,
                               action_preview: Optional[List[str]] = None,
                               payload: Optional[Dict[str, Any]] = None) -> bool:
        """Request human approval for an action.

        DEPRECATED: Use send_message action instead for agent-initiated confirmations.
        This method auto-approves to maintain backward compatibility.
        """
        # Emit telemetry
        event = HumanApprovalRequestedEvent(
            agent_id=self.agent_id,
            step=self.state.n_steps,
            reason=reason,
            required=False,  # Not blocking anymore
            checkpoint_type=checkpoint_type,
            action_preview=action_preview,
            payload=payload,
            timeout_seconds=0
        )
        self.telemetry_manager.capture_event(event)

        # Auto-approve (approval workflow removed)
        self.logger.warning(
            f"request_approval() is deprecated - use send_message action instead. "
            f"Auto-approving: {reason}"
        )
        return True

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
