"""API-specific conversation manager for stateless HTTP requests."""

import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta
from collections import defaultdict, deque

from api.interfaces import (
    IConversationManager, GenericMessage, GenericResponse, MessageContext
)
from utils.bounded_collections import BoundedDict


logger = logging.getLogger(__name__)


class APIConversation:
    """Represents a conversation in the API context."""

    def __init__(self, conversation_id: str, max_history: int = 100):
        """Initialize an API conversation."""
        self.conversation_id = conversation_id
        self.messages: deque = deque(maxlen=max_history)
        self.metadata: Dict[str, Any] = {}
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.message_count = 0

    def add_message(self, message: Union[GenericMessage, GenericResponse]) -> None:
        """Add a message or response to the conversation."""
        self.messages.append({
            "type": "message" if isinstance(message, GenericMessage) else "response",
            "content": message.content if hasattr(message, 'content') else str(message),
            "timestamp": datetime.now(),
            "data": message.to_dict() if hasattr(message, 'to_dict') else {}
        })
        self.last_activity = datetime.now()
        self.message_count += 1

    def get_recent_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent conversation history."""
        return list(self.messages)[-limit:]

    def clear(self) -> None:
        """Clear conversation history."""
        self.messages.clear()
        self.message_count = 0
        self.last_activity = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        """Convert conversation to dictionary."""
        return {
            "conversation_id": self.conversation_id,
            "messages": list(self.messages),
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "message_count": self.message_count
        }


class APIConversationManager(IConversationManager):
    """Manages conversations for API requests.

    This manager is designed for stateless HTTP requests and maintains
    conversation context across requests using conversation IDs.
    """

    def __init__(
        self,
        max_conversations: int = 1000,
        max_history_per_conversation: int = 100,
        conversation_ttl_minutes: int = 60
    ):
        """Initialize the API conversation manager.

        Args:
            max_conversations: Maximum number of concurrent conversations
            max_history_per_conversation: Maximum messages per conversation
            conversation_ttl_minutes: Time-to-live for inactive conversations
        """
        self.conversations: BoundedDict[str, APIConversation] = BoundedDict(
            max_size=max_conversations
        )
        self.max_history = max_history_per_conversation
        self.ttl = timedelta(minutes=conversation_ttl_minutes)
        self.logger = logger

    async def get_or_create_conversation(self, context: MessageContext) -> APIConversation:
        """Get or create a conversation for the given context."""
        conversation_id = context.get_conversation_id()

        # Check if conversation exists and is still valid
        if conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]

            # Check if conversation has expired
            if datetime.now() - conversation.last_activity > self.ttl:
                self.logger.info(f"Conversation {conversation_id} expired, creating new")
                del self.conversations[conversation_id]
            else:
                return conversation

        # Create new conversation
        conversation = APIConversation(conversation_id, self.max_history)
        conversation.metadata = {
            "user_id": context.user_id,
            "chat_id": context.chat_id,
            "platform": context.platform,
            "chat_type": context.chat_type
        }

        self.conversations[conversation_id] = conversation
        self.logger.info(f"Created new conversation: {conversation_id}")

        return conversation

    async def add_message(self, conversation_id: str, message: GenericMessage) -> None:
        """Add a message to the conversation."""
        if conversation_id not in self.conversations:
            # Create conversation from message context
            conversation = await self.get_or_create_conversation(message.context)
        else:
            conversation = self.conversations[conversation_id]

        conversation.add_message(message)
        self.logger.debug(f"Added message to conversation {conversation_id}")

    async def add_response(self, conversation_id: str, response: GenericResponse) -> None:
        """Add a response to the conversation."""
        if conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            conversation.add_message(response)
            self.logger.debug(f"Added response to conversation {conversation_id}")
        else:
            self.logger.warning(f"Conversation {conversation_id} not found for response")

    async def get_history(
        self,
        conversation_id: str,
        limit: int = 10
    ) -> List[Union[GenericMessage, GenericResponse]]:
        """Get conversation history."""
        if conversation_id in self.conversations:
            conversation = self.conversations[conversation_id]
            history = conversation.get_recent_history(limit)

            # Convert history items back to GenericMessage/GenericResponse
            result = []
            for item in history:
                if item["type"] == "message":
                    # Reconstruct GenericMessage from stored data
                    data = item["data"]
                    context_data = data.get("context", {})
                    context = MessageContext(
                        user_id=context_data.get("user_id", "unknown"),
                        chat_id=context_data.get("chat_id", "unknown"),
                        message_id=context_data.get("message_id"),
                        platform=context_data.get("platform", "api"),
                        chat_type=context_data.get("chat_type", "private"),
                        metadata=context_data.get("metadata", {})
                    )
                    message = GenericMessage(
                        content=data.get("content", ""),
                        context=context,
                        attachments=data.get("attachments", []),
                        reply_to=data.get("reply_to")
                    )
                    result.append(message)
                else:
                    # Reconstruct GenericResponse
                    data = item["data"]
                    from api.interfaces import ResponseFormat
                    response = GenericResponse(
                        content=data.get("content", ""),
                        format=ResponseFormat(data.get("format", "text")),
                        metadata=data.get("metadata", {}),
                        attachments=data.get("attachments", []),
                        suggestions=data.get("suggestions", []),
                        callback_data=data.get("callback_data")
                    )
                    result.append(response)

            return result
        return []

    async def clear_conversation(self, conversation_id: str) -> None:
        """Clear a conversation."""
        if conversation_id in self.conversations:
            self.conversations[conversation_id].clear()
            self.logger.info(f"Cleared conversation {conversation_id}")
        else:
            self.logger.warning(f"Conversation {conversation_id} not found")

    async def cleanup_expired(self) -> int:
        """Clean up expired conversations.

        Returns:
            Number of conversations removed
        """
        now = datetime.now()
        expired = []

        for conv_id, conversation in self.conversations.items():
            if now - conversation.last_activity > self.ttl:
                expired.append(conv_id)

        for conv_id in expired:
            del self.conversations[conv_id]

        if expired:
            self.logger.info(f"Cleaned up {len(expired)} expired conversations")

        return len(expired)

    def get_stats(self) -> Dict[str, Any]:
        """Get conversation manager statistics."""
        return {
            "total_conversations": len(self.conversations),
            "max_conversations": self.conversations.max_size,
            "max_history_per_conversation": self.max_history,
            "ttl_minutes": self.ttl.total_seconds() / 60,
            "active_conversations": [
                {
                    "id": conv_id,
                    "message_count": conv.message_count,
                    "last_activity": conv.last_activity.isoformat()
                }
                for conv_id, conv in self.conversations.items()
            ]
        }