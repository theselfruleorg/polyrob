"""
Execution context for tool actions.

This module defines the ActionExecutionContext that encapsulates all
necessary information for executing tool actions, replacing parameter
name introspection with explicit context passing.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.browser.context import BrowserContext


@dataclass
class ActionExecutionContext:
    """
    Context object containing all necessary information for action execution.

    This replaces parameter name introspection in the Registry with explicit
    context passing, improving type safety and maintainability.
    """

    # Core execution resources
    browser_context: Optional["BrowserContext"] = None  # Using string annotation for forward reference

    # Agent identification (CRITICAL for sub-agent isolation)
    agent_id: Optional[str] = None  # ID of the agent executing the action
    is_sub_agent: bool = False  # True for sub-agents to skip side effects
    parent_session_id: Optional[str] = None  # For sub-agents: the main session ID
    # Delegation role: "orchestrator" may delegate; "leaf" may not. H7: default to the
    # LEAST-privileged role — delegation must be granted by explicitly setting
    # role="orchestrator" (the main step loop does), never inherited by a context-less
    # fallback construction.
    role: str = "leaf"

    # Session and user information
    session_id: str = ""  # For sub-agents: this is the virtual session ID
    user_id: Optional[str] = None

    # File system constraints
    workspace_dir: Optional[str] = None
    available_file_paths: List[str] = field(default_factory=list)

    # Security and permissions
    sensitive_data: Dict[str, Any] = field(default_factory=dict)
    allow_file_writes: bool = True
    allow_network_access: bool = True

    # Telemetry and monitoring
    telemetry: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None

    # Additional context
    metadata: Dict[str, Any] = field(default_factory=dict)

    def has_browser(self) -> bool:
        """Check if browser context is available."""
        return self.browser_context is not None

    def validate_file_path(self, path: str) -> bool:
        """
        Validate if a file path is allowed based on constraints.

        Args:
            path: File path to validate

        Returns:
            True if path is allowed, False otherwise
        """
        # Use PathValidator for consistent validation
        from utils.path_validator import PathValidator

        # Always include workspace directory as an allowed path
        allowed_paths = list(self.available_file_paths) if self.available_file_paths else []

        # Add workspace directory to allowed paths if not already included
        if self.workspace_dir and self.workspace_dir not in allowed_paths:
            allowed_paths.append(self.workspace_dir)

        validator = PathValidator(allowed_paths)
        return validator.is_path_allowed(path, self.workspace_dir)

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert context to dictionary for serialization or logging.

        Returns:
            Dictionary representation of the context
        """
        return {
            'agent_id': self.agent_id,
            'is_sub_agent': self.is_sub_agent,
            'role': self.role,
            'session_id': self.session_id,
            'parent_session_id': self.parent_session_id,
            'user_id': self.user_id,
            'workspace_dir': self.workspace_dir,
            'has_browser': self.has_browser(),
            'allow_file_writes': self.allow_file_writes,
            'allow_network_access': self.allow_network_access,
            'trace_id': self.trace_id,
            'available_paths_count': len(self.available_file_paths),
        }

    def clone(self, **updates) -> 'ActionExecutionContext':
        """
        Create a copy of this context with optional updates.

        Args:
            **updates: Fields to update in the cloned context

        Returns:
            New ActionExecutionContext instance
        """
        import copy

        # Start with current values
        kwargs = {
            'browser_context': self.browser_context,
            'agent_id': self.agent_id,
            'is_sub_agent': self.is_sub_agent,
            'role': self.role,
            'parent_session_id': self.parent_session_id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'workspace_dir': self.workspace_dir,
            'available_file_paths': copy.copy(self.available_file_paths),
            'sensitive_data': copy.copy(self.sensitive_data),
            'allow_file_writes': self.allow_file_writes,
            'allow_network_access': self.allow_network_access,
            'telemetry': copy.copy(self.telemetry),
            'trace_id': self.trace_id,
            'metadata': copy.copy(self.metadata),
        }

        # Apply updates
        kwargs.update(updates)

        return ActionExecutionContext(**kwargs)