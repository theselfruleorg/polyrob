"""Agent resource mixin (roadmap P9 decomposition; code-motion from service.py): the
browser-context accessor. ``Agent`` composes ``ResourceMixin`` (call sites unchanged via MRO).
"""
from __future__ import annotations


class ResourceMixin:
    """Browser-context accessor for Agent."""

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

    async def get_used_browser_context(self):
        """The context for a state read, or None until this session has used a page: a
        blank page costs 681 ms per step (healthy Chromium, 102 N-4) and a launch of
        about 200 MB for a step that may never touch the browser. Sticky once used."""
        if not getattr(self, "_browser_page_used", False):
            if not self._has_active_browser_usage():
                return None
            self._browser_page_used = True
        return await self.get_browser_context()

    def actions_need_browser(self, actions) -> bool:
        """True when a planned action, direct or through the ``tool_call`` bridge, belongs
        to the browser tool. The controller refuses such an action with no context."""
        registry = self.controller.registry
        for action in actions or ():
            dump = action.model_dump(exclude_unset=True) if hasattr(action, "model_dump") else {}
            for name, params in (dump or {}).items():
                names = [name]
                if name == "tool_call" and isinstance(params, dict):
                    names.extend(str(v) for v in params.values() if isinstance(v, str))
                for candidate in names:
                    found = registry.get_action(candidate)
                    if getattr(found, "tool", None) == "browser" or candidate.startswith("browser_"):
                        return True
        return False
