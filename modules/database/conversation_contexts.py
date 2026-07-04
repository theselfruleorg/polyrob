from typing import Optional, List, Dict, Any
from datetime import datetime
import json
import logging

from modules.database.connection import DatabaseConnection
from modules.memory.models import ConversationContext, Message


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder for datetime objects."""
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class ConversationContexts:
    """Handles database operations for conversation contexts."""

    def __init__(self, connection: DatabaseConnection):
        """Initialize the conversation contexts handler."""
        self.connection = connection
        self.logger = logging.getLogger('database.conversations')

    async def create_table(self) -> None:
        """Create the conversation_contexts table if it doesn't exist."""
        try:
            await self.connection.execute('''
                CREATE TABLE IF NOT EXISTS conversation_contexts (
                    conversation_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    user_id TEXT,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT,
                    messages TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT DEFAULT '{}',
                    mode TEXT DEFAULT 'active',
                    mode_metadata TEXT DEFAULT '{}',
                    keywords TEXT DEFAULT '[]',
                    last_interaction TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')
            
            # Create indices
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_contexts_user_id 
                ON conversation_contexts(user_id)
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_contexts_chat_id 
                ON conversation_contexts(chat_id)
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_contexts_type 
                ON conversation_contexts(type)
            """)
            await self.connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_contexts_mode 
                ON conversation_contexts(mode)
            """)
            
            self.logger.info("📊 Conversation contexts table and indices verified/created")
        except Exception as e:
            self.logger.error(f"❌ Error creating conversation_contexts table: {str(e)}", exc_info=True)
            raise

    async def get(self, conversation_id: str) -> Optional[ConversationContext]:
        """Retrieve a conversation context by ID."""
        try:
            query = "SELECT * FROM conversation_contexts WHERE conversation_id = ?"
            row = await self.connection.fetch_one(query, (conversation_id,))
            return self._row_to_context(row) if row else None
        except Exception as e:
            self.logger.error(f"Error retrieving conversation context {conversation_id}: {e}", exc_info=True)
            return None

    async def create(self, context: ConversationContext) -> None:
        """Create a new conversation context."""
        try:
            query = """
                INSERT INTO conversation_contexts (
                    conversation_id, type, user_id, chat_id, chat_name,
                    messages, metadata, last_interaction, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            # Combine short-term and long-term memory for storage
            all_messages = []
            if hasattr(context, 'short_term_memory'):
                all_messages.extend(context.short_term_memory)
            if hasattr(context, 'long_term_memory'):
                all_messages.extend(context.long_term_memory)
            
            messages_json = json.dumps(
                [self._serialize_message(msg) for msg in all_messages],
                cls=DateTimeEncoder
            )
            metadata_json = json.dumps(context.metadata)
            
            now = datetime.utcnow()
            context.created_at = context.created_at or now
            context.updated_at = context.updated_at or now
            context.last_interaction = context.last_interaction or now
            
            params = (
                context.conversation_id,
                context.type,
                context.user_id,
                context.chat_id,
                context.chat_name,
                messages_json,
                metadata_json,
                context.last_interaction.isoformat(),
                context.created_at.isoformat(),
                context.updated_at.isoformat()
            )
            
            await self.connection.execute(query, params)
            self.logger.debug(f"Created conversation context: {context.conversation_id}")
            
        except Exception as e:
            self.logger.error(f"Error creating conversation context: {e}", exc_info=True)
            raise

    async def update(self, context: ConversationContext) -> None:
        """Update an existing conversation context."""
        try:
            query = """
                UPDATE conversation_contexts SET
                    type = ?,
                    user_id = ?,
                    chat_id = ?,
                    chat_name = ?,
                    messages = ?,
                    metadata = ?,
                    last_interaction = ?,
                    updated_at = ?
                WHERE conversation_id = ?
            """
            
            # Combine short-term and long-term memory for storage
            all_messages = []
            if hasattr(context, 'short_term_memory'):
                all_messages.extend(context.short_term_memory)
            if hasattr(context, 'long_term_memory'):
                all_messages.extend(context.long_term_memory)
            
            messages_json = json.dumps(
                [self._serialize_message(msg) for msg in all_messages],
                cls=DateTimeEncoder
            )
            metadata_json = json.dumps(context.metadata)
            
            now = datetime.utcnow()
            context.updated_at = now
            context.last_interaction = context.last_interaction or now
            
            params = (
                context.type,
                context.user_id,
                context.chat_id,
                context.chat_name,
                messages_json,
                metadata_json,
                context.last_interaction.isoformat(),
                context.updated_at.isoformat(),
                context.conversation_id
            )
            
            await self.connection.execute(query, params)
            self.logger.debug(f"Updated conversation context: {context.conversation_id}")
            
        except Exception as e:
            self.logger.error(f"Error updating conversation context: {e}", exc_info=True)
            raise

    async def delete(self, conversation_id: str) -> None:
        """Delete a conversation context."""
        try:
            query = "DELETE FROM conversation_contexts WHERE conversation_id = ?"
            await self.connection.execute(query, (conversation_id,))
            self.logger.debug(f"Deleted conversation context: {conversation_id}")
        except Exception as e:
            self.logger.error(f"Error deleting conversation context: {e}", exc_info=True)
            raise

    async def get_all(self) -> List[ConversationContext]:
        """Retrieve all conversation contexts."""
        try:
            query = "SELECT * FROM conversation_contexts"
            rows = await self.connection.fetch_all(query)
            return [self._row_to_context(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error retrieving all conversation contexts: {e}", exc_info=True)
            return []

    async def update_mode(self, conversation_id: str, mode: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Update conversation mode."""
        try:
            await self.connection.execute(
                """
                UPDATE conversation_contexts 
                SET mode = ?, mode_metadata = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE conversation_id = ?
                """,
                (mode, json.dumps(metadata) if metadata else '{}', conversation_id)
            )
            self.logger.debug(f"Updated mode for conversation {conversation_id} to {mode}")
        except Exception as e:
            self.logger.error(f"Error updating mode for conversation {conversation_id}: {e}")
            raise

    async def add_keyword(self, conversation_id: str, keyword: str) -> None:
        """Add a keyword to the conversation."""
        try:
            # Get current keywords
            query = "SELECT keywords FROM conversation_contexts WHERE conversation_id = ?"
            row = await self.connection.fetch_one(query, (conversation_id,))
            current_keywords = json.loads(row['keywords']) if row and row['keywords'] else []
            
            # Add new keyword if not already present
            keyword_lower = keyword.lower()
            if keyword_lower not in current_keywords:
                current_keywords.append(keyword_lower)

            await self.connection.execute(
                """
                UPDATE conversation_contexts 
                SET keywords = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE conversation_id = ?
                """,
                (json.dumps(current_keywords), conversation_id)
            )
            self.logger.debug(f"Added keyword '{keyword}' to conversation {conversation_id}")
        except Exception as e:
            self.logger.error(f"Error adding keyword to conversation {conversation_id}: {e}")
            raise

    async def remove_keyword(self, conversation_id: str, keyword: str) -> None:
        """Remove a keyword from the conversation."""
        try:
            # Get current keywords
            query = "SELECT keywords FROM conversation_contexts WHERE conversation_id = ?"
            row = await self.connection.fetch_one(query, (conversation_id,))
            current_keywords = json.loads(row['keywords']) if row and row['keywords'] else []
            
            # Remove keyword
            keyword_lower = keyword.lower()
            current_keywords = [k for k in current_keywords if k != keyword_lower]

            await self.connection.execute(
                """
                UPDATE conversation_contexts 
                SET keywords = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE conversation_id = ?
                """,
                (json.dumps(current_keywords), conversation_id)
            )
            self.logger.debug(f"Removed keyword '{keyword}' from conversation {conversation_id}")
        except Exception as e:
            self.logger.error(f"Error removing keyword from conversation {conversation_id}: {e}")
            raise

    async def get_keywords(self, conversation_id: str) -> List[str]:
        """Get all keywords for a conversation."""
        try:
            query = "SELECT keywords FROM conversation_contexts WHERE conversation_id = ?"
            row = await self.connection.fetch_one(query, (conversation_id,))
            if row and row['keywords']:
                return json.loads(row['keywords'])
            return []
        except Exception as e:
            self.logger.error(f"Error getting keywords for conversation {conversation_id}: {e}")
            return []

    def _row_to_context(self, row: Dict[str, Any]) -> ConversationContext:
        """Convert database row to ConversationContext."""
        try:
            # Parse timestamps
            created_at = datetime.fromisoformat(row['created_at']) if row.get('created_at') else datetime.utcnow()
            updated_at = datetime.fromisoformat(row['updated_at']) if row.get('updated_at') else datetime.utcnow()
            last_interaction = (
                datetime.fromisoformat(row['last_interaction']) 
                if row.get('last_interaction') 
                else datetime.utcnow()
            )

            # Parse JSON fields with defaults
            try:
                messages_data = json.loads(row.get('messages', '[]'))
                messages = [
                    Message.from_dict(msg) if isinstance(msg, dict) else msg
                    for msg in messages_data
                ]
            except (json.JSONDecodeError, TypeError):
                messages = []

            try:
                metadata = json.loads(row.get('metadata', '{}'))
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            try:
                mode_metadata = json.loads(row.get('mode_metadata', '{}'))
            except (json.JSONDecodeError, TypeError):
                mode_metadata = {}

            try:
                keywords = json.loads(row.get('keywords', '[]'))
            except (json.JSONDecodeError, TypeError):
                keywords = []

            return ConversationContext(
                conversation_id=row['conversation_id'],
                type=row['type'],
                chat_id=row['chat_id'],
                user_id=row.get('user_id'),
                chat_name=row.get('chat_name'),
                messages=messages,
                metadata=metadata,
                mode=row.get('mode', 'active'),
                mode_metadata=mode_metadata,
                keywords=keywords,
                last_interaction=last_interaction,
                created_at=created_at,
                updated_at=updated_at
            )

        except Exception as e:
            self.logger.error(f"Error converting row to context: {e}")
            raise

    async def get_by_type(self, context_type: str) -> List[ConversationContext]:
        """Retrieve all conversation contexts of a specific type."""
        try:
            query = "SELECT * FROM conversation_contexts WHERE type = ?"
            rows = await self.connection.fetch_all(query, (context_type,))
            return [self._row_to_context(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error retrieving contexts by type {context_type}: {e}", exc_info=True)
            return []

    async def get_by_chat_id(self, chat_id: str) -> Optional[ConversationContext]:
        """Retrieve a conversation context by chat_id."""
        try:
            query = "SELECT * FROM conversation_contexts WHERE chat_id = ?"
            row = await self.connection.fetch_one(query, (chat_id,))
            return self._row_to_context(row) if row else None
        except Exception as e:
            self.logger.error(f"Error retrieving context by chat_id {chat_id}: {e}", exc_info=True)
            return None

    async def get_by_user_id(self, user_id: str) -> List[ConversationContext]:
        """Retrieve all conversation contexts for a specific user."""
        try:
            query = "SELECT * FROM conversation_contexts WHERE user_id = ?"
            rows = await self.connection.fetch_all(query, (user_id,))
            return [self._row_to_context(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error retrieving contexts for user {user_id}: {e}", exc_info=True)
            return []

    async def update_last_interaction(self, conversation_id: str, timestamp: datetime) -> None:
        """Update the last interaction timestamp for a conversation."""
        try:
            query = """
                UPDATE conversation_contexts 
                SET last_interaction = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE conversation_id = ?
            """
            await self.connection.execute(query, (timestamp.isoformat(), conversation_id))
            self.logger.debug(f"Updated last interaction for conversation: {conversation_id}")
        except Exception as e:
            self.logger.error(f"Error updating last interaction for conversation {conversation_id}: {e}", exc_info=True)
            raise

    def _serialize_message(self, message: Message) -> Dict[str, Any]:
        """Serialize a message object to a dictionary."""
        if hasattr(message, 'to_dict'):
            return message.to_dict()
        elif hasattr(message, 'dict'):
            msg_dict = message.dict()
            if msg_dict.get('timestamp'):
                msg_dict['timestamp'] = msg_dict['timestamp'].isoformat()
            return msg_dict
        else:
            return {
                'content': getattr(message, 'content', ''),
                'role': getattr(message, 'role', 'user'),
                'sender_id': getattr(message, 'sender_id', ''),
                'timestamp': getattr(message, 'timestamp', datetime.utcnow()).isoformat(),
                'metadata': getattr(message, 'metadata', {})
            }

    async def get_by_id(self, conversation_id: str) -> Optional[ConversationContext]:
        """Get conversation context by ID."""
        return await self.get(conversation_id)

    async def save(self, context: ConversationContext) -> None:
        """Save conversation context (upsert)."""
        try:
            query = """
                INSERT INTO conversation_contexts (
                    conversation_id, type, user_id, chat_id, chat_name,
                    messages, metadata, mode, mode_metadata, keywords,
                    last_interaction, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    messages = excluded.messages,
                    metadata = excluded.metadata,
                    mode = excluded.mode,
                    mode_metadata = excluded.mode_metadata,
                    keywords = excluded.keywords,
                    last_interaction = excluded.last_interaction,
                    updated_at = CURRENT_TIMESTAMP
            """
            
            # Convert messages to proper format
            messages_json = json.dumps(
                [msg.to_dict() if hasattr(msg, 'to_dict') else msg for msg in context.messages],
                cls=DateTimeEncoder
            )
            
            await self.connection.execute(
                query,
                (
                    context.conversation_id,
                    context.type,
                    context.user_id,
                    context.chat_id,
                    context.chat_name,
                    messages_json,
                    json.dumps(context.metadata, cls=DateTimeEncoder),
                    context.mode,
                    json.dumps(context.mode_metadata, cls=DateTimeEncoder),
                    json.dumps(context.keywords),
                    context.last_interaction.isoformat()
                )
            )
        except Exception as e:
            self.logger.error(f"Error saving context {context.conversation_id}: {e}")
            raise
