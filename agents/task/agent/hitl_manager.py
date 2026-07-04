"""
Human-in-the-Loop (HITL) Manager for Task Agents

Centralizes all human interaction logic.
"""

from __future__ import annotations
import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Any

#: OR-7: scrub brain-state from streamed chunks at this single funnel (default ON).
#: Set STREAM_BRAIN_SCRUB=off/false/0/no to restore the legacy all-or-nothing guard.
_STREAM_BRAIN_SCRUB = os.getenv("STREAM_BRAIN_SCRUB", "true").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)


class HITLManager:
    """Manages human-in-the-loop interactions for task agents."""

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        max_messages_per_minute: int = 10,
        rate_limit_window: int = 60,
        logger: Optional[logging.Logger] = None,
        telemetry_manager: Optional[Any] = None
    ):
        """Initialize HITL manager with simplified configuration."""
        self.session_id = session_id
        self.agent_id = agent_id
        self.telemetry_manager = telemetry_manager

        # Logger setup (allow injection or create session logger)
        if logger:
            self.logger = logger
        else:
            from agents.task.logging_config import get_task_logger
            self.logger = get_task_logger("hitl", session_id)

        # Message queue
        self._user_messages: deque = deque(maxlen=100)
        self._message_lock = asyncio.Lock()
        self._max_user_messages_per_step = 3

        # Rate limiting
        self._message_timestamps: deque = deque(maxlen=100)
        self._max_messages_per_minute = max_messages_per_minute
        self._rate_limit_window = rate_limit_window

        # Streaming output
        self._output_callbacks = []
        self._stream_lock = asyncio.Lock()
        self._callback_failures = 0

    async def queue_user_message(
        self,
        text: str,
        kind: str = "comment",
        metadata: Optional[Dict[str, Any]] = None,
        skip_rate_limit: bool = False
    ) -> None:
        """Queue user message for next step.

        Args:
            text: Message text
            kind: Message type (comment, correction, guidance, approval, rejection)
            metadata: Optional metadata
            skip_rate_limit: Skip rate limiting (for system messages)

        Raises:
            ValueError: If rate limit exceeded
        """
        if metadata is None:
            metadata = {}

        # Rate limiting (unless skipped for system messages)
        if not skip_rate_limit:
            now = time.time()

            # Remove old timestamps outside the window
            while self._message_timestamps and now - self._message_timestamps[0] > self._rate_limit_window:
                self._message_timestamps.popleft()

            # Check if limit exceeded
            if len(self._message_timestamps) >= self._max_messages_per_minute:
                raise ValueError(
                    f"Rate limit exceeded: maximum {self._max_messages_per_minute} "
                    f"messages per {self._rate_limit_window} seconds"
                )

            self._message_timestamps.append(now)

        # Queue the message
        async with self._message_lock:
            self._user_messages.append({
                "text": text,
                "kind": kind,
                "metadata": metadata,
                "timestamp": datetime.utcnow()
            })
            self.logger.info(f"Queued {kind} message: {text[:100]}...")

        # NOTE: FEED event for UI display is now emitted by the API endpoint
        # (task_http_api.py send_user_message) to prevent duplicate messages.
        # The API emits immediately when the request is received, which is better
        # for UI responsiveness than waiting for the agent to queue it.

        # Emit TELEMETRY event for analytics (separate from feed)
        if self.telemetry_manager:
            try:
                from agents.task.telemetry.views import UserMessageDuringExecutionEvent
                event = UserMessageDuringExecutionEvent(
                    agent_id=self.agent_id,
                    step=metadata.get("step", 0),
                    message_text=text[:200],  # Truncate for privacy
                    message_kind=kind,
                    queue_depth=len(self._user_messages),
                    execution_phase="running"
                )
                self.telemetry_manager.capture_event(event)
            except Exception as e:
                self.logger.debug(f"Failed to emit telemetry: {e}")

        # Emit queue status update for UI indicator
        await self._emit_queue_status()

    async def drain_user_messages(self) -> List[Dict[str, Any]]:
        """Drain queued messages for injection."""
        async with self._message_lock:
            if not self._user_messages:
                return []
            messages = []
            for _ in range(min(len(self._user_messages), self._max_user_messages_per_step)):
                messages.append(self._user_messages.popleft())
            if self._user_messages:
                self.logger.info(f"Drained {len(messages)}, {len(self._user_messages)} remaining")

        # Emit queue status update after drain
        await self._emit_queue_status()

        return messages

    async def _emit_queue_status(self) -> None:
        """Emit queue status event for UI updates.

        Called after queue changes to update the chat UI indicator.
        """
        if not self.telemetry_manager:
            return

        try:
            from agents.task.telemetry.views import QueueStatusEvent

            # Calculate oldest message age if queue not empty
            oldest_age = None
            if self._user_messages:
                oldest_msg = self._user_messages[0]
                if isinstance(oldest_msg.get('timestamp'), datetime):
                    oldest_age = (datetime.utcnow() - oldest_msg['timestamp']).total_seconds()

            event = QueueStatusEvent(
                agent_id=self.agent_id,
                queued_count=len(self._user_messages),
                processing=False,
                oldest_message_age_seconds=oldest_age
            )
            self.telemetry_manager.capture_event(event)
        except Exception as e:
            self.logger.debug(f"Failed to emit queue status: {e}")

    async def get_recent_messages(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent messages without consuming."""
        async with self._message_lock:
            messages = list(self._user_messages)
            messages.reverse()
            return messages[:limit]

    def get_queue_size(self) -> int:
        """Get number of queued messages.

        Returns:
            Number of messages in queue
        """
        return len(self._user_messages)

    def has_streaming_callbacks(self) -> bool:
        """Check if streaming callbacks are registered.

        Returns:
            True if any callbacks registered
        """
        return len(self._output_callbacks) > 0

    def get_callback_count(self) -> int:
        """Get number of registered streaming callbacks.

        Returns:
            Number of active callbacks
        """
        return len(self._output_callbacks)

    def get_stats(self) -> Dict[str, Any]:
        """Get HITL statistics."""
        return {
            'queued_messages': self.get_queue_size(),
            'streaming_callbacks': self.get_callback_count(),
            'callback_failures': self._callback_failures
        }

    def register_output_callback(self, callback) -> None:
        """Register callback for streaming output.

        Args:
            callback: async function(chunk: str)
        """
        if callback not in self._output_callbacks:
            self._output_callbacks.append(callback)

    async def stream_output(self, chunk: str) -> None:
        """Stream output chunk to callbacks.

        Brain-state telemetry is filtered out here — the single funnel for every
        stream consumer (CLI / API / WebView).  POLYROB asks every model to emit its
        ``{"current_state": {...}}`` brain state as text content; that is not the
        agent's voice and must not be streamed (it otherwise surfaces as a raw
        JSON dump on a tool-free planning turn for any streaming provider).

        Args:
            chunk: Text chunk to stream
        """
        if not self._output_callbacks:
            return

        # Suppress agent brain-state JSON (internal telemetry, not user-facing).
        # OR-7: scrub brain blocks out of the chunk (handles fenced / mixed-with-
        # prose / truncated shapes the legacy whole-string guard missed), keeping
        # any real prose. Fail-open: on any scrub error, fall back to the legacy
        # all-or-nothing guard so a genuine reply is never dropped.
        chunk_to_send = chunk
        if _STREAM_BRAIN_SCRUB:
            try:
                from modules.llm.brain_scrubber import scrub_brain_blocks
                scrubbed = scrub_brain_blocks(chunk)
                # Drop only when the chunk was *wholly* brain-state telemetry —
                # scrub_brain_blocks returns "" (empty string) in that case.
                # A whitespace-only chunk (e.g. " " between tokens) is legitimate
                # content and must NOT be dropped: scrub returns it unchanged.
                if scrubbed is None or scrubbed == "":
                    return  # chunk was wholly brain-state telemetry
                chunk_to_send = scrubbed
            except Exception:
                from agents.task.utils_json import is_brain_state_content
                if is_brain_state_content(chunk):
                    return
        else:
            from agents.task.utils_json import is_brain_state_content
            if is_brain_state_content(chunk):
                return

        async with self._stream_lock:
            for callback in self._output_callbacks:
                try:
                    await callback(chunk_to_send)
                except Exception as e:
                    self._callback_failures += 1
                    self.logger.error(f"Streaming callback error (total: {self._callback_failures}): {e}")

    async def wait_for_user_input(
        self,
        prompt: str,
        timeout: int = 300
    ) -> Optional[str]:
        """Wait for user to respond to agent's question.

        Agent calls this to ask user a question and wait for answer.

        Args:
            prompt: Question to ask user
            timeout: Seconds to wait (default 5 min)

        Returns:
            User's response or None if timeout
        """
        self.logger.info(f"Waiting for user: {prompt[:100]}...")

        start_time = time.time()
        while time.time() - start_time < timeout:
            # Check for messages
            messages = await self.drain_user_messages()
            if messages:
                answer = messages[0]['text']
                response_time = time.time() - start_time
                self.logger.info(f"User responded: {answer[:100]}...")

                # Emit telemetry event
                if self.telemetry_manager:
                    try:
                        from agents.task.telemetry.views import AgentQuestionEvent
                        event = AgentQuestionEvent(
                            agent_id=self.agent_id,
                            step=0,  # Will be updated by agent if needed
                            question_prompt=prompt[:200],
                            response_time_seconds=response_time,
                            timed_out=False,
                            user_response=answer[:200]
                        )
                        self.telemetry_manager.capture_event(event)
                    except Exception as e:
                        self.logger.debug(f"Failed to emit telemetry: {e}")

                return answer

            await asyncio.sleep(1)

        self.logger.warning(f"User input timeout after {timeout}s")

        # Emit telemetry event for timeout
        if self.telemetry_manager:
            try:
                from agents.task.telemetry.views import AgentQuestionEvent
                event = AgentQuestionEvent(
                    agent_id=self.agent_id,
                    step=0,
                    question_prompt=prompt[:200],
                    response_time_seconds=timeout,
                    timed_out=True,
                    user_response=None
                )
                self.telemetry_manager.capture_event(event)
            except Exception as e:
                self.logger.debug(f"Failed to emit telemetry: {e}")

        return None

    def get_state(self) -> Dict[str, Any]:
        """Serialize HITL manager state for persistence.

        Returns:
            State dictionary containing all persisted state
        """
        return {
            "queued_messages": [
                {
                    "text": msg["text"],
                    "kind": msg["kind"],
                    "metadata": msg["metadata"],
                    "timestamp": msg["timestamp"].isoformat() if isinstance(msg["timestamp"], datetime) else msg["timestamp"]
                }
                for msg in self._user_messages
            ],
            "callbacks_count": len(self._output_callbacks),
            "callback_failures": self._callback_failures
        }

    def restore_state(self, state: Dict[str, Any]) -> None:
        """Restore HITL manager state from persistence.

        Args:
            state: State dictionary from get_state()
        """
        # Restore message queue
        self._user_messages.clear()
        for msg in state.get("queued_messages", []):
            # Convert timestamp string back to datetime if needed
            if isinstance(msg.get("timestamp"), str):
                try:
                    msg["timestamp"] = datetime.fromisoformat(msg["timestamp"])
                except (ValueError, TypeError):
                    msg["timestamp"] = datetime.utcnow()
            self._user_messages.append(msg)

        self._callback_failures = state.get("callback_failures", 0)

        self.logger.info(f"Restored {len(self._user_messages)} queued messages")

    def clear_all_queues(self) -> None:
        """Clear all message queues for cleanup."""
        self._user_messages.clear()
        self._message_timestamps.clear()
        self.logger.debug("Cleared all HITL message queues")

    def clear_callbacks(self) -> None:
        """Clear all streaming callbacks for cleanup."""
        self._output_callbacks.clear()
        self._callback_failures = 0
        self.logger.debug("Cleared all streaming callbacks")
