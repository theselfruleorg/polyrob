"""HITL ingress mixin (roadmap P9; code-motion from orchestrator.py).

User-message submission and approval-decision ingress on the session orchestrator
(thin delegations over HITLManager). Split out so SessionOrchestrator owns session
lifecycle; it composes HITLIngressMixin. External callers (task_agent_lite, api) use
orchestrator.submit_user_message / record_decision etc. unchanged.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional

# C1: context-reference expansion is an ALLOWLIST of trusted human-intake kinds.
# Anything not listed — forged self_wake/delegation_result, future system/synthetic
# kinds, etc. — is never expanded (expansion is a filesystem read on caller text).
_TRUSTED_CONTEXT_REF_KINDS = {"comment", "continuation"}

# B5 (2026-07-13 review): one lock for all taint mutations. The flag was a bare bool
# raced by the owner-turn clear and the correspondent-inject set; contention is
# negligible (a few writes per turn), so a module-level lock keeps it simple.
_TAINT_LOCK = threading.Lock()


class HITLIngressMixin:
    """User-message + approval-decision ingress for SessionOrchestrator."""

    # --- correspondent taint (B5) ------------------------------------------
    def _set_correspondent_taint(self, surface: Optional[str], address: Optional[str]) -> None:
        """Mark the session correspondent-tainted, recording WHO tainted it so the
        scoped reply exemption (D1) can allow answering exactly that party."""
        src = (surface or "", (address or "").strip().lower())
        with _TAINT_LOCK:
            if not hasattr(self, "_correspondent_taint_sources"):
                self._correspondent_taint_sources = set()
            self._correspondent_taint_sources.add(src)
            self._correspondent_tainted = True

    def _clear_correspondent_taint(self) -> None:
        """A genuine owner turn re-opens the gate — clear the flag AND the sources."""
        with _TAINT_LOCK:
            self._correspondent_tainted = False
            self._correspondent_taint_sources = set()

    async def submit_user_message(
        self,
        agent_id: Optional[str],
        text: str,
        kind: str = "comment",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Submit user message to target agent's HITL manager.

        Routes user message to appropriate agent for processing in next step.
        
        Raises:
            MessageQueueFullError: If message queue is full (HTTP 429 - retryable)
        """
        from core.exceptions import MessageQueueFullError

        if metadata is None:
            metadata = {}

        # WS-A: a genuine owner/continuation turn clears any correspondent taint — the
        # owner is driving again, so the capability gate re-opens high-impact tools.
        if kind in _TRUSTED_CONTEXT_REF_KINDS:
            self._clear_correspondent_taint()

        # C1: expand @file/@folder/@diff/@url references for trusted human intake only
        # (allowlist). Forged/system kinds are never expanded — see _TRUSTED_CONTEXT_REF_KINDS.
        if kind in _TRUSTED_CONTEXT_REF_KINDS:
            try:
                from agents.task.constants import AutonomyConfig
                if AutonomyConfig.context_references_enabled():
                    from agents.task.agent.messages.context_references import (
                        preprocess_context_references,
                    )
                    from agents.task.path import pm
                    _root = str(pm().get_workspace_dir(self.session_id, self.user_id))
                    text = preprocess_context_references(
                        text, root=_root, confine_to_root=True, allow_filesystem=True
                    )
            except Exception:
                pass  # fail-soft: leave text unchanged

        # Get queue limit from environment
        MAX_QUEUED_MESSAGES = int(os.environ.get('MAX_QUEUED_MESSAGES', '10'))

        # Get target agent
        if agent_id:
            agent = self.agents.get(agent_id)
            if not agent:
                self.logger.warning(f"Agent {agent_id} not found")
                return
        else:
            # Route to first available agent
            if not self.agents:
                # CRITICAL FIX: Store in pending queue instead of dropping!
                # Messages will be delivered when agent is created
                # SECURITY FIX: Use lock to prevent race with create_agent
                async with self._pending_messages_lock:
                    # Bound the pre-agent queue too — without this the MessageQueueFullError
                    # backpressure only applied once an agent existed, so a recreated-but-
                    # not-yet-stepped session could accumulate unbounded pending messages.
                    if len(self._pending_messages) >= MAX_QUEUED_MESSAGES:
                        raise MessageQueueFullError(
                            f"Pending message queue full "
                            f"({len(self._pending_messages)}/{MAX_QUEUED_MESSAGES}). "
                            f"Wait for the agent to start processing.",
                            queue_size=len(self._pending_messages),
                            max_size=MAX_QUEUED_MESSAGES,
                        )
                    has_images = bool(metadata and metadata.get('image_attachments'))
                    self.logger.info(
                        f"📦 No agents yet - storing message in pending queue "
                        f"(has_images={has_images}, pending_count={len(self._pending_messages) + 1})"
                    )
                    self._pending_messages.append((text, kind, metadata))
                return
            agent = next(iter(self.agents.values()))

        # Check queue size before adding message
        if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
            # Get current queue size
            queue_size = agent.hitl_manager.get_queue_size()
            
            if queue_size >= MAX_QUEUED_MESSAGES:
                raise MessageQueueFullError(
                    f"Message queue full ({queue_size}/{MAX_QUEUED_MESSAGES}). "
                    f"Wait for agent to process pending messages.",
                    queue_size=queue_size,
                    max_size=MAX_QUEUED_MESSAGES
                )
            
            # Queue the message
            await agent.hitl_manager.queue_user_message(text, kind, metadata)
            self.logger.info(f"Routed message to {agent.agent_id} (queue: {queue_size + 1}/{MAX_QUEUED_MESSAGES})")
        elif hasattr(agent, 'receive_user_message'):
            # Fallback for old agents
            await agent.receive_user_message(text, kind, metadata)
        else:
            self.logger.warning(f"Agent {agent.agent_id} cannot receive messages")

    def inject_correspondent_message(
        self,
        text: str,
        source: str,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        surface: Optional[str] = None,
        address: Optional[str] = None,
    ) -> bool:
        """WS-A: inject a third-party correspondent reply as DATA, not a steering turn.

        A correspondent's reply enters as a CORRESPONDENT-origin CONTROL message
        (``<correspondent-message>`` envelope + inner ``<untrusted_tool_result>`` wrap),
        the same non-obey channel hierarchical memory uses — NOT the user "PRIORITY
        INPUT" guidance frame. Pushed as a one-shot ephemeral message so the running
        loop reads it on its next step. Returns True if delivered to a resident agent,
        False if none is available (the caller drops + audits — the honest behaviour).
        """
        if not getattr(self, "agents", None):
            return False
        agent = next(iter(self.agents.values()))
        mm = getattr(agent, "message_manager", None)
        if mm is None or not hasattr(mm, "push_ephemeral_message"):
            return False
        from modules.llm.messages import make_control_message, MessageOrigin
        from agents.task.agent.core.untrusted_wrap import wrap_untrusted
        # Delimiter-injection defense (Fusion HIGH): neutralize any closing fence the
        # correspondent embeds, so they can't break out of the untrusted/correspondent
        # blocks and present forged text as trusted instruction.
        safe = (text or "")
        for _tag in ("</untrusted_tool_result>", "</correspondent-message>"):
            safe = safe.replace(_tag, _tag.replace("<", "&lt;").replace(">", "&gt;"))
            safe = safe.replace(_tag.upper(), _tag.replace("<", "&lt;").replace(">", "&gt;"))
        msg = make_control_message(wrap_untrusted(source, safe), MessageOrigin.CORRESPONDENT)
        mm.push_ephemeral_message(msg)
        # Taint the session: the capability gate now blocks high-impact tools until the
        # owner drives again (cleared in submit_user_message on a genuine owner turn).
        # The source label doubles as the address when the caller doesn't split them.
        self._set_correspondent_taint(surface, address or source)
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(f"Injected correspondent DATA from {source} (ephemeral, untrusted)")
        return True

    async def record_decision(
        self,
        agent_id: str,
        approved: bool,
        note: Optional[str] = None
    ) -> bool:
        """Record approval decision for an agent.

        Simplified: Just forwards to agent which injects as user guidance.
        """
        agent = self.agents.get(agent_id)
        if not agent:
            self.logger.warning(f"Agent {agent_id} not found")
            return False

        # Forward to agent (simplified - no HITLManager method anymore)
        if hasattr(agent, 'record_approval_decision'):
            await agent.record_approval_decision(approved, note)
            self.logger.info(f"Recorded {'approval' if approved else 'rejection'}")
            return True
        else:
            self.logger.warning(f"Agent {agent.agent_id} cannot process decisions")
            return False

    async def get_pending_messages(self) -> List[Dict[str, Any]]:
        """DEPRECATED: Orchestrator no longer stores messages."""
        self.logger.debug("get_pending_messages deprecated - use agent HITL manager")
        return []
    
    async def get_recent_messages(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent messages from active agent.

        DEPRECATED: Use agent's HITL manager directly.
        """
        # Proxy to first available agent
        if self.agents:
            agent = next(iter(self.agents.values()))
            if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                return await agent.hitl_manager.get_recent_messages(limit)
        return []

