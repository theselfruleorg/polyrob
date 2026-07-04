"""Unified exception hierarchy for the tools system.

All tool-related exceptions should inherit from ToolSystemError.
This provides consistent error handling across the tools subsystem.
"""

from typing import Optional, Dict, Any, List


class ToolSystemError(Exception):
    """Base exception for all tool system errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class ToolNotFoundError(ToolSystemError):
    """Raised when a requested tool is not found."""

    def __init__(self, tool_name: str, available_tools: Optional[List[str]] = None):
        details = {"tool_name": tool_name}
        if available_tools:
            details["available_tools"] = available_tools
        super().__init__(f"Tool '{tool_name}' not found", details)
        self.tool_name = tool_name


class ActionNotFoundError(ToolSystemError):
    """Raised when a requested action is not found."""

    def __init__(self, action_name: str, tool_name: Optional[str] = None,
                 available_actions: Optional[List[str]] = None):
        details = {"action_name": action_name}
        if tool_name:
            details["tool_name"] = tool_name
        if available_actions:
            details["available_actions"] = available_actions[:10]  # Limit for readability

        msg = f"Action '{action_name}' not found"
        if tool_name:
            msg += f" in tool '{tool_name}'"
        super().__init__(msg, details)
        self.action_name = action_name
        self.tool_name = tool_name


class ActionValidationError(ToolSystemError):
    """Raised when action parameters fail validation."""

    def __init__(self, action_name: str, errors: List[str],
                 params: Optional[Dict[str, Any]] = None):
        details = {
            "action_name": action_name,
            "validation_errors": errors
        }
        if params:
            details["provided_params"] = params
        super().__init__(
            f"Validation failed for action '{action_name}': {'; '.join(errors)}",
            details
        )
        self.action_name = action_name
        self.errors = errors


class ActionExecutionError(ToolSystemError):
    """Raised when action execution fails."""

    def __init__(self, action_name: str, cause: str,
                 original_error: Optional[Exception] = None):
        details = {"action_name": action_name, "cause": cause}
        if original_error:
            details["original_error"] = str(original_error)
            details["error_type"] = type(original_error).__name__
        super().__init__(f"Failed to execute action '{action_name}': {cause}", details)
        self.action_name = action_name
        self.original_error = original_error


class DuplicateActionError(ToolSystemError):
    """Raised when attempting to register a duplicate action."""

    def __init__(self, action_name: str, existing_tool: Optional[str] = None,
                 new_tool: Optional[str] = None):
        details = {"action_name": action_name}
        if existing_tool:
            details["existing_tool"] = existing_tool
        if new_tool:
            details["new_tool"] = new_tool
        super().__init__(f"Action '{action_name}' is already registered", details)
        self.action_name = action_name


class SchemaGenerationError(ToolSystemError):
    """Raised when schema generation fails."""

    def __init__(self, action_name: str, provider: str, cause: str):
        details = {
            "action_name": action_name,
            "provider": provider,
            "cause": cause
        }
        super().__init__(
            f"Failed to generate {provider} schema for '{action_name}': {cause}",
            details
        )
        self.action_name = action_name
        self.provider = provider


class ToolInitializationError(ToolSystemError):
    """Raised when a tool fails to initialize."""

    def __init__(self, tool_name: str, cause: str,
                 missing_deps: Optional[List[str]] = None,
                 missing_config: Optional[List[str]] = None):
        details = {"tool_name": tool_name, "cause": cause}
        if missing_deps:
            details["missing_dependencies"] = missing_deps
        if missing_config:
            details["missing_config"] = missing_config
        super().__init__(f"Tool '{tool_name}' failed to initialize: {cause}", details)
        self.tool_name = tool_name
        self.missing_deps = missing_deps
        self.missing_config = missing_config


class ToolCleanupError(ToolSystemError):
    """Raised when tool cleanup fails."""

    def __init__(self, tool_name: str, cause: str):
        super().__init__(f"Tool '{tool_name}' failed to cleanup: {cause}",
                        {"tool_name": tool_name, "cause": cause})
        self.tool_name = tool_name


class MCPError(ToolSystemError):
    """Base exception for MCP-related errors."""
    pass


class MCPServerError(MCPError):
    """Raised for MCP server connection/communication errors."""

    def __init__(self, server_name: str, cause: str):
        super().__init__(
            f"MCP server '{server_name}' error: {cause}",
            {"server_name": server_name, "cause": cause}
        )
        self.server_name = server_name


class MCPToolExecutionError(MCPError):
    """Raised when MCP tool execution fails."""

    def __init__(self, server_name: str, tool_name: str, cause: str):
        super().__init__(
            f"MCP tool '{tool_name}' on server '{server_name}' failed: {cause}",
            {"server_name": server_name, "tool_name": tool_name, "cause": cause}
        )
        self.server_name = server_name
        self.tool_name = tool_name
