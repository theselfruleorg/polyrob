"""Generic interfaces for API-agent communication.

These interfaces provide platform-agnostic abstractions for message handling,
allowing the system to work with multiple communication channels (HTTP, WebSocket, etc.).
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MessageType(Enum):
    """Types of messages supported by the system."""
    TEXT = "text"
    COMMAND = "command"
    CALLBACK = "callback"
    MEDIA = "media"
    SYSTEM = "system"


class ResponseFormat(Enum):
    """Response format types."""
    TEXT = "text"
    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"
    STREAM = "stream"


@dataclass
class MessageContext:
    """Context information for a message."""
    user_id: str
    chat_id: str
    message_id: Optional[str] = None
    platform: str = "api"  # api, web
    chat_type: str = "private"  # private only
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_conversation_id(self) -> str:
        """Generate a unique conversation ID."""
        if self.chat_type == "private":
            return f"{self.platform}_{self.user_id}"
        return f"{self.platform}_{self.chat_id}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary."""
        return {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "platform": self.platform,
            "chat_type": self.chat_type,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata
        }


@dataclass
class GenericMessage:
    """Platform-agnostic message representation."""
    content: str
    context: MessageContext
    type: MessageType = MessageType.TEXT
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    reply_to: Optional[str] = None

    @property
    def is_command(self) -> bool:
        """Check if message is a command."""
        return self.type == MessageType.COMMAND or self.content.startswith("/")

    def get_command_parts(self) -> tuple[str, str]:
        """Extract command and arguments."""
        if not self.is_command:
            return "", ""

        text = self.content.strip()
        if text.startswith("/"):
            text = text[1:]

        parts = text.split(None, 1)
        command = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return command, args

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary."""
        return {
            "content": self.content,
            "type": self.type.value,
            "context": self.context.to_dict(),
            "attachments": self.attachments,
            "reply_to": self.reply_to
        }


@dataclass
class GenericResponse:
    """Platform-agnostic response representation."""
    content: str
    format: ResponseFormat = ResponseFormat.TEXT
    metadata: Dict[str, Any] = field(default_factory=dict)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    callback_data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert response to dictionary."""
        return {
            "content": self.content,
            "format": self.format.value,
            "metadata": self.metadata,
            "attachments": self.attachments,
            "suggestions": self.suggestions,
            "callback_data": self.callback_data
        }


class IMessageProcessor(ABC):
    """Interface for components that process messages."""

    @abstractmethod
    async def process_message(self, message: GenericMessage) -> GenericResponse:
        """Process a message and return a response."""
        pass

    @abstractmethod
    async def can_handle(self, message: GenericMessage) -> bool:
        """Check if this processor can handle the message."""
        pass


class IConversationManager(ABC):
    """Interface for conversation management."""

    @abstractmethod
    async def get_or_create_conversation(self, context: MessageContext) -> Any:
        """Get or create a conversation for the given context."""
        pass

    @abstractmethod
    async def add_message(self, conversation_id: str, message: GenericMessage) -> None:
        """Add a message to the conversation."""
        pass

    @abstractmethod
    async def add_response(self, conversation_id: str, response: GenericResponse) -> None:
        """Add a response to the conversation."""
        pass

    @abstractmethod
    async def get_history(self, conversation_id: str, limit: int = 10) -> List[Union[GenericMessage, GenericResponse]]:
        """Get conversation history."""
        pass

    @abstractmethod
    async def clear_conversation(self, conversation_id: str) -> None:
        """Clear a conversation."""
        pass


class IAgent(ABC):
    """Interface for AI agents."""

    @abstractmethod
    async def process(self, message: GenericMessage, conversation: Any = None) -> GenericResponse:
        """Process a message with optional conversation context."""
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the agent."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the agent."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> List[str]:
        """List of agent capabilities."""
        pass


class IMessageRouter(ABC):
    """Interface for message routing."""

    @abstractmethod
    async def route(self, message: GenericMessage) -> IAgent:
        """Route a message to the appropriate agent."""
        pass

    @abstractmethod
    async def register_agent(self, pattern: str, agent: IAgent) -> None:
        """Register an agent for a routing pattern."""
        pass

    @abstractmethod
    async def unregister_agent(self, pattern: str) -> None:
        """Unregister an agent."""
        pass