"""SessionContext Protocol — typed contract for tool-session communication.

Tools depend on this Protocol instead of reaching through SessionOrchestrator.
SessionOrchestrator satisfies it by structural subtyping (no inheritance needed).
"""

from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionContext(Protocol):
    """Minimum interface tools need from a session.

    Derived from audited reach-through sites in:
    - tools/controller/service.py (orchestrator.session_id, .user_id, .workspace_dir,
      .agents, .session_manager.add_to_feed, .sub_agent_manager, .telemetry_manager)
    - tools/mcp/mcp_tool.py (orchestrator.agents, .workspace_dir)
    """

    @property
    def session_id(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    @property
    def workspace_dir(self) -> str: ...

    async def add_to_feed(self, agent_id: str, entry_type: str, data: dict) -> None:
        """Write an entry to the session feed (delegates to session_manager)."""
        ...

    def get_agents(self) -> Dict[str, Any]:
        """Return the dict of active agents in this session."""
        ...

    def get_tool_call_tracker(self) -> Optional[Any]:
        """Return the ToolCallTracker for this session, or None."""
        ...

    def get_sub_agent_manager(self) -> Optional[Any]:
        """Return the SubAgentManager for this session, or None."""
        ...

    def get_telemetry_manager(self) -> Optional[Any]:
        """Return the TelemetryManager for this session, or None."""
        ...
