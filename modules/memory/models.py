"""Core data models for memory management."""

from typing import Dict, Any, Optional, List, Union
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from dataclasses import dataclass, field


class MessageRole(Enum):
    """Enum for message roles."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class Message:
    """Represents a single message."""
    content: str
    role: str = "user"
    sender_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary."""
        return {
            'content': self.content,
            'role': self.role,
            'sender_id': self.sender_id,
            'timestamp': self.timestamp.isoformat(),
            'metadata': self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Message':
        """Create message from dictionary."""
        if isinstance(data.get('timestamp'), str):
            data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        return cls(**data)

    def __json__(self) -> Dict[str, Any]:
        """JSON serialization support."""
        return self.to_dict()


@dataclass
class UserProfile:
    """User profile data model (wallet-based, clean 1.0.0 schema)."""
    # IDENTIFIERS
    user_id: str                                           # Primary key (deterministic from wallet)
    wallet_address: str                                    # Ethereum wallet address
    
    # OPTIONAL PROFILE
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    
    # AUTHORIZATION
    role: str = 'user'                                     # user/admin
    tier: str = 'free'                                     # free/holder/x402/admin
    
    # WALLET TRACKING
    current_wallet_chain: Optional[str] = 'ethereum'        # ethereum/polygon/etc
    current_wallet_connected_at: Optional[datetime] = None
    
    # TOKEN OWNERSHIP
    den_token_count: int = 0                               # NFT holdings
    den_token_verified_at: Optional[datetime] = None
    
    # METADATA
    total_sessions: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConversationContext(BaseModel):
    """Represents a conversation context."""
    # Core identifiers
    conversation_id: str = Field(..., description="Unique identifier for the conversation")
    chat_id: str = Field(..., description="Chat identifier")
    type: str = Field("private", description="Type of conversation (private/group)")
    user_id: Optional[str] = Field(None, description="User identifier")
    chat_name: Optional[str] = Field(None, description="Chat name")
    
    # Memory components
    messages: List[Message] = Field(
        default_factory=list,
        description="All conversation messages"
    )
    short_term_memory: List[Message] = Field(
        default_factory=list,
        description="Recent messages (last 10)"
    )
    long_term_memory: List[Message] = Field(
        default_factory=list,
        description="Historical messages"
    )
    working_memory: Dict[str, Any] = Field(
        default_factory=dict,
        description="Current conversation variables"
    )
    
    # State and metadata
    mode: str = Field(default="active", description="Conversation mode")
    mode_metadata: Dict[str, Any] = Field(default_factory=dict)
    keywords: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_interaction: datetime = Field(default_factory=datetime.utcnow)


    def add_message(self, message: Union[Dict[str, Any], Message]) -> None:
        """Add a message to the context."""
        self.last_interaction = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        
        # Convert dict to Message if needed
        if isinstance(message, dict):
            message = Message(**message)
        
        # Add to messages list
        self.messages.append(message)
        
        # Add to short-term memory
        self.short_term_memory.append(message)
        
        # Move excess messages to long-term memory
        while len(self.short_term_memory) > 10:
            oldest_message = self.short_term_memory.pop(0)
            self.long_term_memory.append(oldest_message)
            
        # Trim long-term memory if it gets too large
        max_long_term = 1000
        if len(self.long_term_memory) > max_long_term:
            self.long_term_memory = self.long_term_memory[-max_long_term:]

    def get_recent_messages(self, limit: int = 5) -> List[Message]:
        """Get the most recent messages."""
        return self.messages[-limit:] if self.messages else []

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary format."""
        return {
            'conversation_id': self.conversation_id,
            'chat_id': self.chat_id,
            'type': self.type,
            'user_id': self.user_id,
            'chat_name': self.chat_name,
            'messages': [msg.to_dict() for msg in self.messages],
            'short_term_memory': [msg.to_dict() for msg in self.short_term_memory],
            'long_term_memory': [msg.to_dict() for msg in self.long_term_memory],
            'working_memory': self.working_memory,
            'mode': self.mode,
            'mode_metadata': self.mode_metadata,
            'keywords': self.keywords,
            'metadata': self.metadata,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'last_interaction': self.last_interaction.isoformat()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ConversationContext':
        """Create a ConversationContext instance from a dictionary."""
        # Convert message dictionaries to Message objects
        for field_name in ['messages', 'short_term_memory', 'long_term_memory']:
            if field_name in data:
                data[field_name] = [
                    Message.from_dict(msg) if isinstance(msg, dict) else msg 
                    for msg in data[field_name]
                ]
        
        # Convert timestamp strings to datetime objects
        for field_name in ['created_at', 'updated_at', 'last_interaction']:
            if isinstance(data.get(field_name), str):
                data[field_name] = datetime.fromisoformat(data[field_name])

        return cls(**data)
