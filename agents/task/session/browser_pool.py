from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from tools.browser.browser import Browser


class BrowserPoolMixin:
    """Browser-pool access, tools dict compatibility, and context release for SessionOrchestrator."""

    @property
    def tools(self) -> Dict[str, Any]:
        """Backward compatibility: Return tools as a dict."""
        if not self.controller:
            return {}
        result = {}
        for tool_name in self.controller.list_tools():
            tool = self.controller.get_tool(tool_name)
            if tool:
                result[tool_name] = tool
        return result

    @tools.setter
    def tools(self, value: Dict[str, Any]) -> None:
        """Backward compatibility: Set tools from a dict."""
        if not self.controller:
            self.logger.warning("Controller not initialized - cannot set tools")
            return
        # Clear existing tools
        for tool_name in self.controller.list_tools():
            self.controller.remove_tool(tool_name)
        # Add all new tools
        for name, tool in value.items():
            self.controller.add_tool(name, tool)
            self.logger.debug(f"Added tool '{name}' to controller")

    async def get_browser_context(self, agent_id: str, reuse: bool = True, timeout: float = 30.0) -> Optional[Any]:
        """Get browser context for agent from BrowserManager.

        Args:
            agent_id: Agent ID (format: agentname_sessionid)
            reuse: Whether to reuse pooled contexts
            timeout: Unused (kept for compatibility)

        Returns:
            Browser context or None
        """
        # No BrowserManager is an EXPECTED steady state now — the default CLI (and
        # any non-browser session) provisions none, and the step loop asks for a
        # context every step regardless of loaded tools. Log at debug, not error
        # (OR-3): an error here is misleading noise on every step of a normal run.
        if not self.browser_manager:
            self.logger.debug(f"No BrowserManager - no browser context for {agent_id}")
            return None

        # Agent ID already contains session (format set in Agent.__init__)
        # No need to manipulate - use as-is
        context = await self.browser_manager.get_context(agent_id, reuse=reuse)

        # Track context for cleanup (prevents leaks if agent removed before cleanup)
        if context and hasattr(self, '_browser_contexts'):
            self._browser_contexts.add(agent_id)
            self.logger.debug(f"Tracking browser context for cleanup: {agent_id}")

        # Track context acquisition with telemetry
        if context and hasattr(self, 'telemetry_manager') and self.telemetry_manager:
            try:
                self.telemetry_manager.capture_event(
                    event_type="browser_context_acquired",
                    data={
                        "agent_id": agent_id,
                        "session_id": self.session_id,
                        "context_id": id(context),
                        "reused": reuse
                    }
                )
            except Exception as e:
                self.logger.debug(f"Failed to emit browser context telemetry: {e}")

        return context

    async def _ensure_browser_available(self) -> Optional['Browser']:
        """Get browser from BrowserManager (single source of truth).

        Returns:
            Browser instance from BrowserManager, or None if unavailable
        """
        if not self.browser_manager:
            self.logger.error("BrowserManager required but not available")
            return None

        # Initialize BrowserManager if needed
        if not self.browser_manager.is_initialized:
            try:
                await self.browser_manager.initialize()
            except Exception as e:
                self.logger.error(f"Failed to initialize BrowserManager: {e}")
                return None

        # Get browser from BrowserManager (ONLY source)
        try:
            browser = await self.browser_manager.get_browser()
            if browser:
                self.logger.debug("Got browser from BrowserManager")
                return browser
            else:
                self.logger.error("BrowserManager has no browser available")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get browser from BrowserManager: {e}")
            return None

    async def release_browser_context(self, context_id: str, close: bool = True):
        """Release a browser context back to the pool or close it.

        Args:
            context_id: ID of the context to release
            close: If True, close the context instead of returning to pool

        This delegates to BrowserManager if available, otherwise is a no-op.
        """
        if self.browser_manager:
            try:
                await self.browser_manager.release_context(context_id, close=close)
                self.logger.debug(f"Released browser context {context_id} (close={close})")
            except Exception as e:
                self.logger.warning(f"Failed to release browser context {context_id}: {e}")
        else:
            self.logger.debug(f"No browser_manager available to release context {context_id}")
