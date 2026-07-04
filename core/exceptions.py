"""Location: core/exceptions.py"""

"""Core exceptions for the bot."""

class BotError(Exception):
    """Base class for all bot-related errors."""
    pass

class ComponentError(BotError):
    """Base for all component-related errors."""
    pass

class ContainerError(BotError):
    """Raised when there's an error in the dependency container."""
    pass

class ComponentInitializationError(ComponentError):
    """Component initialization failed."""
    pass

class ComponentCleanupError(ComponentError):
    """Component cleanup failed."""
    pass

class ConfigurationError(ComponentError):
    """Configuration validation failed."""
    pass

class DependencyError(ComponentError):
    """Dependency validation or resolution failed."""
    pass

class ServiceError(ComponentError):
    """Base class for service-related errors."""
    pass

class ToolError(ComponentError):
    """Base class for tool-related errors."""
    pass

class HandlerError(ComponentError):
    """Base class for handler-related errors."""
    pass

class AgentError(ComponentError):
    """Base class for agent-related errors."""
    pass

class SessionOwnershipError(AgentError):
    """Raised when a caller tries to (re)create/reuse a session_id that already
    belongs to a different user (C4 — cross-user session hijack/DoS). The API layer
    maps this to HTTP 403."""
    pass

class LLMError(ComponentError):
    """Base class for LLM-related errors."""
    pass

class LLMConfigError(LLMError, ConfigurationError):
    """LLM configuration error."""
    pass

class LLMConnectionError(LLMError):
    """LLM connection error."""
    pass

class LLMResponseError(LLMError):
    """LLM response validation error."""
    pass

class LLMRateLimitError(LLMError):
    """LLM rate limit exceeded."""
    pass

class LLMAuthenticationError(LLMError):
    """LLM authentication failed."""
    pass

class LLMContextLengthError(LLMError):
    """LLM context length exceeded."""
    pass

class LLMInvalidRequestError(LLMError):
    """LLM invalid request error."""
    pass

class LLMPermanentError(LLMError):
    """LLM error that should NOT be retried (auth, quota, billing).
    
    Sessions should halt immediately and NOT attempt fallback retries.
    Use this for errors that indicate account-level issues rather than
    transient failures.
    
    Examples:
        - Invalid API key
        - Quota exhausted / billing issue
        - Account suspended
    """
    pass

class LLMProviderExhaustedError(LLMError):
    """Raised when all LLM providers have been tried and failed.
    
    This indicates the fallback chain has been exhausted.
    """
    def __init__(self, message: str, providers_tried: list = None):
        self.providers_tried = providers_tried or []
        super().__init__(message)

class DatabaseError(ComponentError):
    """Base class for database-related errors."""
    pass

class PermissionError(ComponentError):
    """Permission-related errors (bot-specific)."""
    pass

class ValidationError(ComponentError):
    """Validation-related errors."""
    pass

class APIError(BotError):
    """Raised when there's an error communicating with external APIs."""
    pass

class AuthenticationError(APIError):
    """Raised when authentication fails with an external service."""
    pass

class RateLimitError(APIError):
    """Raised when rate limits are exceeded for an external service."""
    def __init__(self, message: str, service: str = None, wait_time: int = None):
        self.service = service
        self.wait_time = wait_time
        super().__init__(message)

class ResourceNotFoundError(APIError):
    """Raised when a requested resource is not found."""
    pass

class ConversationError(BotError):
    """Raised when there's an error in conversation handling."""
    pass

class StorageError(BotError):
    """Raised when there's an error with data storage operations."""
    pass


class PromptError(BotError):
    """Raised when there is an error with prompt operations."""
    pass

class EmbeddingError(BotError):
    """Raised when there is an error with embedding operations."""
    pass

class CacheError(BotError):
    """Raised when there is an error with cache operations."""
    pass

class BadRequestError(Exception):
    """Raised when a request is malformed or invalid."""
    pass

class MemoryStorageError(BotError):
    """Raised when there is an error with memory storage operations."""
    pass

class KnowledgeBaseError(BotError):
    """Raised when there is an error with the knowledge base operations."""
    pass


class ManagerError(BotError):
    """Manager error."""
    pass

class PermissionsError(BotError):
    """Permissions error."""
    pass

class MemoryError(BotError):
    """Memory error."""
    pass

"""Custom exceptions for the bot."""

class ModuleError(BotError):
    """Module related errors."""
    pass

class ConfigError(Exception):
    """Raised when there is an error in the configuration."""
    pass

class ModelError(Exception):
    """Raised when there is an error with a model."""
    pass

# MCP (Model Context Protocol) specific exceptions
class MCPError(ServiceError):
    """Base class for MCP-related errors."""
    pass

class MCPConnectionError(MCPError):
    """Raised when there's an error connecting to an MCP server."""
    pass

class MCPProtocolError(MCPError):
    """Raised when there's an error in MCP protocol communication."""
    pass

class MCPToolExecutionError(MCPError):
    """Raised when there's an error executing an MCP tool."""
    pass


# Session and HITL (Human-in-the-Loop) specific exceptions
class SessionError(AgentError):
    """Base class for session-related errors."""
    pass


class MessageQueueFullError(SessionError):
    """Raised when a session's message queue is full.

    This is a retryable error - the client should wait and retry.
    HTTP status code: 429 (Too Many Requests)
    """

    def __init__(self, message: str, queue_size: int = None, max_size: int = None):
        self.queue_size = queue_size
        self.max_size = max_size
        super().__init__(message)


class SessionNotFoundError(SessionError):
    """Raised when a session is not found."""
    pass


class SessionStateError(SessionError):
    """Raised when a session is in an invalid state for the requested operation."""
    pass


# Auth / tier / billing domain exceptions.
# Kept in core so agents/modules can raise them without depending on api/ or
# fastapi. The API layer (api/app.py) translates these to HTTPException
# responses via global exception handlers.

class AuthError(BotError):
    """Base class for auth/identity errors raised by modules/auth/.

    Carries an HTTP-style status_code hint that the API layer uses to map
    domain errors to HTTP responses. Defaults to 403 (forbidden).
    """
    status_code: int = 403


class UserNotFoundError(AuthError):
    """Raised when a user profile cannot be found."""
    status_code = 404


class TierError(AuthError):
    """Raised for invalid tier or tier-related access failures."""
    status_code = 403


class InsufficientCreditsError(BotError):
    """Raised when a user's credit balance is insufficient for an operation.

    Agents catch this to suspend execution and surface the funding state to
    the user. The API layer translates it to HTTP 402 Payment Required.
    """
    status_code = 402

    def __init__(self, user_id: str, required: int, available: int, message: str = None):
        self.user_id = user_id
        self.required = required
        self.available = available
        super().__init__(
            message
            or f"Insufficient credits for user {user_id}. Required: {required}, Available: {available}. Please add credits to continue."
        )