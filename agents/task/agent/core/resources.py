"""Agent resource-management mixin (roadmap P9 decomposition; code-motion from service.py).

Bounded-collection memory management (init + cleanup) and the browser-context
accessor. Moved verbatim off the ``Agent`` god-file; ``Agent`` composes
``ResourceMixin`` (call sites unchanged via MRO).
"""
from __future__ import annotations

import os
from typing import Dict


class ResourceMixin:
    """Memory-management (bounded collections) + browser-context accessor for Agent."""

    def _initialize_memory_management(self) -> None:
        """Initialize memory management using bounded collections."""
        from utils.bounded_collections import BoundedDict

        # Use BoundedDict for automatic LRU eviction
        self._telemetry_requests = BoundedDict(max_size=100)
        self._file_references = BoundedDict(max_size=500)

        # NOTE: User message queue removed - managed by HITLManager
        self._max_user_guidance_tokens = int(os.getenv("MAX_USER_GUIDANCE_TOKENS", "1000"))

        self.logger.debug("Initialized memory management with bounded collections")

    def cleanup_memory(self, force: bool = False) -> Dict[str, int]:
        """Clean up memory using bounded collections.

        Args:
            force: If True, perform aggressive cleanup regardless of thresholds

        Returns:
            Dictionary with cleanup statistics
        """
        from datetime import timedelta

        cleanup_stats = {}

        try:
            # 1. Telemetry cleanup - BoundedDict handles eviction automatically
            if hasattr(self, '_telemetry_requests') and force:
                removed = self._telemetry_requests.cleanup_old(max_age=timedelta(minutes=5))
                if removed > 0:
                    cleanup_stats['telemetry_entries_removed'] = removed

            # 2. File references cleanup - BoundedDict handles eviction automatically
            if hasattr(self, '_file_references') and force:
                removed = self._file_references.cleanup_old(max_age=timedelta(hours=1))
                if removed > 0:
                    cleanup_stats['files_cleaned'] = removed

            # 3. Browser state history (keep existing logic)
            if hasattr(self, '_last_browser_states') and len(self._last_browser_states) > 3:
                while len(self._last_browser_states) > 3:
                    self._last_browser_states.popleft()
                    cleanup_stats['states_removed'] = cleanup_stats.get('states_removed', 0) + 1

            # 4. Force garbage collection if requested
            if force:
                import gc
                collected = gc.collect()
                cleanup_stats['gc_collected'] = collected

            # Log results
            total_cleaned = sum(v for k, v in cleanup_stats.items() if k != 'gc_collected')
            if total_cleaned > 10 or force:
                self.logger.info(f"Memory cleanup removed {total_cleaned} items: {cleanup_stats}")
            elif total_cleaned > 0:
                self.logger.debug(f"Memory cleanup: {cleanup_stats}")

            return cleanup_stats

        except Exception as e:
            self.logger.error(f"Error during memory cleanup: {e}", exc_info=True)
            return cleanup_stats

    async def get_browser_context(self):
        """Get browser context from orchestrator.

        BrowserManager maintains context cache internally (contexts_in_use),
        so calling this multiple times returns the same context for this agent_id.

        Returns:
            Current browser context or None if not available
        """
        try:
            return await self.orchestrator.get_browser_context(self.agent_id)
        except Exception as e:
            self.logger.debug(f"Failed to get browser context: {e}")
            return None
