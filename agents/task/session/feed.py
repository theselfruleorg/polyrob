from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

_logger = logging.getLogger(__name__)


def build_stream_publish(
    router: Any, session_key: Optional[str]
) -> Callable[..., Awaitable[None]]:
    """Return an async ``(chunk, step) -> None`` that mirrors a streamed chunk into
    the unified outbound seam (``MessageRouter.publish``) as a partial OutboundMessage.

    P1a outbound-collapse: additive and gated on ``SINGULAR_CHAT_ENABLED``. When the
    flag is OFF, or there is no router / session_key, the returned fn is a no-op so
    the legacy stream callback path stays byte-identical. Fail-open — a publish error
    never propagates into the run loop.
    """
    async def _publish(chunk: str, step: int = 0) -> None:
        from agents.task.surface_config import SurfaceConfig

        if not SurfaceConfig.singular_chat_enabled():
            return
        if router is None or not session_key:
            return
        try:
            from core.surfaces.envelopes import OutboundMessage

            await router.publish(OutboundMessage(
                session_key=session_key,
                text=chunk,
                partial=True,
                # Stable per-TURN stream_id (the session_key, NOT per-step): all of a
                # turn's deltas edit ONE live message under incremental streaming, and the
                # turn's discrete reply finalizes that same stream (see Surface
                # _finalize_live_on_send). A per-step id would open a new bubble each step
                # and never finalize. `step` is retained for callers but no longer keys.
                stream_id=session_key,
            ))
        except Exception as e:  # fail-open: streaming mirror is non-critical
            _logger.debug("stream publish mirror failed: %s", e)

    return _publish


class FeedMixin:
    """Feed metadata processing and stream-callback wiring for SessionOrchestrator."""

    def _process_feed_metadata(self, clean_id, feed_entries: list) -> None:
        """Extract and store metadata from feed entries.

        Args:
            clean_id: Cleaned session ID
            feed_entries: List of feed entry data objects
        """
        if not feed_entries:
            return

        try:
            # Initialize metadata update
            metadata_update = {
                "feed": {
                    "processed_entries": len(feed_entries),
                    "last_processed": datetime.now().isoformat(),
                    "entries": []
                }
            }

            # Process each entry and extract key information
            for entry in feed_entries:
                # Skip entries without proper type
                if not isinstance(entry, dict) or "type" not in entry:
                    continue

                entry_type = entry.get("type", "unknown")
                timestamp = entry.get("timestamp", time.time())

                # Create basic entry metadata
                entry_meta = {
                    "type": entry_type,
                    "timestamp": timestamp
                }

                # Extract specific data based on entry type
                if entry_type == "step" and "data" in entry:
                    # Step data - extract actions and progress
                    data = entry.get("data", {})
                    step_num = entry.get("step", 0)

                    # Add step-specific metadata
                    entry_meta.update({
                        "step": step_num,
                        "actions": [action.get("name", action.get("action_type", "unknown"))
                                   for action in data.get("actions", [])],
                        "has_errors": bool(data.get("errors")),
                        "task_progress": data.get("task_progress", "")
                    })

                elif entry_type == "planner" and "data" in entry:
                    # Planner data
                    data = entry.get("data", {})
                    step_num = entry.get("step", 0)

                    # Add planner-specific metadata
                    entry_meta.update({
                        "step": step_num,
                        "model": data.get("model_name", "unknown"),
                        "components": data.get("components", {})
                    })

                elif entry_type == "evaluation" and "data" in entry:
                    # Evaluation data
                    data = entry.get("data", {})
                    step_num = entry.get("step", 0)

                    # Add evaluation-specific metadata
                    entry_meta.update({
                        "step": step_num,
                        "model": data.get("model_name", "unknown"),
                        "assessment": data.get("assessment", ""),
                        "has_strengths": bool(data.get("strengths")),
                        "has_weaknesses": bool(data.get("weaknesses")),
                        "has_suggestions": bool(data.get("suggestions"))
                    })

                elif entry_type == "multi_agent_relationship" and "data" in entry:
                    # Relationship data
                    data = entry.get("data", {})

                    # Add relationship-specific metadata
                    entry_meta.update({
                        "agent_count": len(data.get("agent_ids", [])),
                        "agent_types": data.get("agent_types", {}),
                        "has_sequence": bool(data.get("agent_sequence"))
                    })

                # Add the entry metadata to the list
                metadata_update["feed"]["entries"].append(entry_meta)

            # Update session metadata with the feed information
            if self.session_manager:
                self.session_manager.update_session_metadata(clean_id, metadata_update)
                self.logger.debug(f"Updated session metadata with {len(feed_entries)} feed entries")

        except Exception as e:
            self.logger.warning(f"Error processing feed metadata: {e}", exc_info=True)

    async def _register_stream_callback(self, agent: Any) -> None:
        """Wire the injected stream callback into the agent's HITL manager.

        The callback transport (httpx → webview, websocket, in-memory queue,
        etc.) is supplied by whoever constructed the orchestrator. Core code
        never knows the destination; it only forwards each LLM output chunk
        with metadata.
        """
        callback = self._on_stream_chunk
        # P1a outbound-collapse: mirror chunks into the unified MessageRouter seam when
        # a router + chat session_key have been bound (later phases). No-op otherwise,
        # so the legacy callback path stays byte-identical until then.
        _mirror = build_stream_publish(
            getattr(self, "_message_router", None),
            getattr(self, "_chat_session_key", None),
        )
        if callback is None:
            # Even with no legacy callback, the unified mirror may be active.
            async def stream_wrapper(chunk: str) -> None:
                await _mirror(chunk, getattr(agent, 'n_steps', 0))
            try:
                agent.hitl_manager.register_output_callback(stream_wrapper)
            except Exception as e:
                self.logger.warning(f"Failed to register stream callback: {e}")
            return

        async def stream_wrapper(chunk: str) -> None:
            try:
                await callback(
                    self.session_id,
                    agent.agent_id,
                    chunk,
                    getattr(agent, 'n_steps', 0),
                )
            except Exception as e:
                # Streaming is non-critical; never fail the run for transport errors.
                self.logger.debug(f"Stream callback error: {e}")
            await _mirror(chunk, getattr(agent, 'n_steps', 0))

        try:
            agent.hitl_manager.register_output_callback(stream_wrapper)
            self.logger.info(f"✅ Stream callback registered for {agent.agent_id}")
        except Exception as e:
            self.logger.warning(f"Failed to register stream callback: {e}")

    async def add_to_feed(self, agent_id: str, entry_type: str, data: dict) -> None:
        if self.session_manager:
            await self.session_manager.add_to_feed(
                self.session_id, agent_id, entry_type, data
            )
