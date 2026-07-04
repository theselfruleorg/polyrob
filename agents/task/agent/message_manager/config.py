"""Configuration for MessageManager to support dependency injection."""

from dataclasses import dataclass
from typing import Optional, List, Dict, Type

# Native message types
from modules.llm.messages import SystemMessage

from agents.task.constants import IMG_TOKENS


@dataclass
class MessageManagerConfig:
    """Configuration object for MessageManager initialization.

    This groups related configuration to simplify MessageManager constructor
    and support dependency injection patterns.

    UNIFIED CONFIG: Now includes all message-related settings including limits
    from TaskSessionConfig to avoid duplication and explicit parameter passing.
    """
    # Token and context limits (unified from TaskSessionConfig)
    image_tokens: int = IMG_TOKENS
    max_input_tokens: int = 128000  # From TaskSessionConfig.limits
    max_actions_per_step: int = 10  # From TaskSessionConfig.limits

    # Message handling
    max_error_length: int = 400
    include_attributes: List[str] = None

    # Context and session
    message_context: Optional[str] = None
    sensitive_data: Optional[Dict[str, str]] = None
    session_id: Optional[str] = None

    # Behavior flags
    include_examples: bool = True
    add_task_message: bool = True
    use_native_tools: bool = True  # T-02: Default to True for OpenAI/Gemini

    # Optional prebuilt system message
    system_message: Optional[SystemMessage] = None

    @classmethod
    def from_session_config(cls, session_config: 'TaskSessionConfig', **overrides) -> 'MessageManagerConfig':
        """Create MessageManagerConfig from TaskSessionConfig.

        This eliminates the need to pass max_input_tokens and max_actions_per_step
        explicitly by extracting them from the session config.

        Args:
            session_config: The TaskSessionConfig containing limits
            **overrides: Additional overrides for config fields

        Returns:
            MessageManagerConfig with values from session config
        """
        from agents.task.config import TaskSessionConfig

        config_dict = {}

        # Extract limits from session config
        if hasattr(session_config, 'limits'):
            config_dict['max_input_tokens'] = session_config.limits.max_input_tokens or 128000
            config_dict['max_actions_per_step'] = session_config.limits.max_actions_per_step

        # Apply any overrides
        config_dict.update(overrides)

        return cls(**config_dict)

    def __post_init__(self):
        """Set defaults for mutable fields."""
        if self.include_attributes is None:
            self.include_attributes = []
        if self.sensitive_data is None:
            self.sensitive_data = {}

    def to_dict(self) -> dict:
        """Convert to dictionary for backward compatibility.

        Now includes all fields including limits.
        """
        return {
            "image_tokens": self.image_tokens,
            "max_input_tokens": self.max_input_tokens,
            "max_actions_per_step": self.max_actions_per_step,
            "max_error_length": self.max_error_length,
            "include_attributes": self.include_attributes,
            "message_context": self.message_context,
            "sensitive_data": self.sensitive_data,
            "session_id": self.session_id,
            "include_examples": self.include_examples,
            "add_task_message": self.add_task_message,
            "use_native_tools": self.use_native_tools,
            "system_message": self.system_message,
        }