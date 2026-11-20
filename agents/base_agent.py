"""Base agent class for all bot agents."""

import logging
import asyncio
from typing import List, Optional, Dict, Any, Union, TYPE_CHECKING, Awaitable
from abc import abstractmethod

from core.base_component import BaseComponent
from core.config import BotConfig
from core.container import DependencyContainer
from core.exceptions import AgentError
from core.version import get_version
from modules.memory.models import Message
from modules.llm.billing_guard import fresh_credential
from modules.llm.llm_client import LLMClient

if TYPE_CHECKING:
    from agents.personality.character import Character
    from modules.memory.memory_manager import MemoryManager
    # Already imported above, just keeping for type checking
    # from modules.llm.llm_client import LLMClient

class BaseAgent(BaseComponent):
    """Base class for all agents."""

    def __init__(self, *, config: BotConfig, container: DependencyContainer, name: str):
        """Initialize base agent."""
        super().__init__(name=name, config=config, container=container)
        
        # Initialize core components - all will be properly set in _initialize()
        self.llm_client = None
        self.character_manager = None
        self.active_character = None
        self.memory_manager = None
        
        # Initialize state
        self._initialized = False
        self._enabled = True
        self._last_result = None
        self._init_lock = asyncio.Lock()
        
        # LLM config with character settings if available
        self.llm_config = {
            'model_type': 'claude-sonnet-5-5',
            'temperature': 0.7,
            'max_tokens': 4096,
            'top_p': 0.9,
            'top_k': 50
        }
        
        # Update with character settings if available
        if self.active_character and hasattr(self.active_character, 'settings'):
            self.llm_config.update(self.active_character.settings)

    async def _initialize(self) -> None:
        """Initialize agent base components."""
        try:
            # Initialize LLM client
            if self.container:
                # Try getting preferred LLM client first, falling back to any available client
                self.llm_client = self._get_preferred_llm_client()
                if not self.llm_client:
                    self.logger.warning("Preferred LLM client not available, will try to use any available client")
                    self.llm_client = self.container.get_service('llm')
                    if not self.llm_client:
                        self.logger.warning("Primary LLM client not available, will try alternative clients")
                        # Try specific clients in order of preference
                        for client_name in ['anthropic_client', 'openai_client']:
                            if self.container.has_service(client_name):
                                self.llm_client = self.container.get_service(client_name)
                                self.logger.info(f"Using {client_name} as fallback")
                                break
                
                if not self.llm_client:
                    self.logger.error("No LLM client available - agent functionality will be limited")
                
                # Get optional services properly, only if they exist
                if self.container.has_service('memory_manager'):
                    self.memory_manager = self.container.get_service('memory_manager')
                
                # Get character manager and role-specific character
                if self.container.has_service('character_manager'):
                    self.character_manager = self.container.get_service('character_manager')
                    try:
                        self.active_character = await self.character_manager.get_character_for_role(self.name)
                        if self.active_character:
                            self.logger.debug(f"Using character: {self.active_character.name}")
                        else:
                            self.logger.debug("No specific character assigned, using default settings")
                    except Exception as e:
                        self.logger.debug(f"Could not load character: {e}, using default settings")
                
                self.logger.info(f"{self.name} agent base components initialized")
            else:
                raise AgentError("Container is required for agent initialization")
            
        except Exception as e:
            self.logger.error(f"Error initializing agent base components: {e}")
            raise

    def _get_preferred_llm_client(self) -> Optional[LLMClient]:
        """Get preferred LLM client based on agent configuration."""
        if not self.container:
            return None
            
        # The config's choice, else the primary the LLMManager already chose from the
        # keys and the operator's pin. A fixed provider name here was missing on every
        # box without that provider's key.
        preferred_client = self.config.get(self.name, {}).get('llm_client')
        if not preferred_client:
            preferred_client = getattr(self.container.get_service('llm'),
                                       'primary_client_name', None)
        if not preferred_client:
            return None

        # Try to get the preferred client
        client = self.container.get_service(preferred_client)
        if client:
            self.logger.info(f"Using preferred LLM client: {preferred_client}")
            return client
            
        # If preferred client is not available, use any client
        return None

    async def _cleanup(self) -> None:
        """Clean up agent resources."""
        try:
            if self.llm_client:
                await self.llm_client.cleanup()
            self.logger.info(f"{self.name} agent cleaned up")
        except Exception as e:
            self.logger.error(f"Error cleaning up {self.name} agent: {e}")

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'llm_client': 'LLM service for text generation',
            'memory_manager': 'Memory management service'
        }

    async def initialize(self) -> None:
        """Initialize agent and its components."""
        try:
            await self._initialize()
            if self.active_character:
                self.logger.info(f"{self.name} agent initialized with character {self.active_character.name}")
            else:
                self.logger.info(f"{self.name} agent initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize {self.name} agent: {str(e)}")
            raise AgentError(f"Failed to initialize agent: {str(e)}")

    @abstractmethod
    async def process_input(
        self,
        input_text: str,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Process input and generate response."""
        raise NotImplementedError("Subclasses must implement process_input")

    @abstractmethod
    async def start_conversation(
        self,
        user_id: str,
        chat_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Start a new conversation."""
        raise NotImplementedError("Subclasses must implement start_conversation")

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up agent resources."""
        pass

    async def generate_response(
        self,
        messages: Optional[List[Message]] = None,
        prompt: Optional[Union[str, Dict[str, Any]]] = None,
        system: Optional[str] = None,
        context: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """Generate response using LLM."""
        try:
            if not self.llm_client:
                self.logger.error("No LLM client available")
                return "I'm unable to respond right now due to a configuration issue. Please try again later."
                
            # Get character info if available
            character_info = None
            if self.active_character:
                character_info = {
                    "name": self.active_character.name,
                    "bio": self.active_character.bio,
                    "traits": self.active_character.adjectives,
                    "style": self.active_character.style
                }
            
            # Determine appropriate parameters based on LLM client type
            llm_client_type = self.llm_client.__class__.__name__.lower()
            model_name = getattr(self.llm_client, 'model_type', 'unknown')
            
            # Get current values from the client itself
            client_temperature = getattr(self.llm_client, 'temperature', None)
            client_max_tokens = getattr(self.llm_client, 'max_tokens', None)
            
            # Use passed values, client values, or config defaults - in that order of priority
            request_temperature = temperature or client_temperature or self.llm_config.get('temperature', 0.7)
            request_max_tokens = max_tokens or client_max_tokens or self.llm_config.get('max_tokens', 4096)
            
            # Log request parameters
            self.logger.info(f"LLM Request | Model: {model_name} | Client: {llm_client_type} | " +
                           f"Temp: {request_temperature} | Max Tokens: {request_max_tokens}")
            
            # Initialize with default parameters
            llm_params = {
                "temperature": request_temperature,
                "max_tokens": request_max_tokens
            }
            
            # Format messages based on client type
            formatted_messages = []
            
            # If messages is a list of Message objects, convert to dict format
            if messages and isinstance(messages, list):
                if messages and isinstance(messages[0], dict):
                    # Already in dict format
                    formatted_messages = messages
                else:
                    for msg in messages:
                        if hasattr(msg, 'to_dict'):
                            formatted_messages.append(msg.to_dict())
                        elif hasattr(msg, 'role') and hasattr(msg, 'content'):
                            formatted_messages.append({
                                'role': msg.role,
                                'content': msg.content
                            })
            # If prompt is provided as a dict with messages
            elif prompt and isinstance(prompt, dict) and 'messages' in prompt:
                for msg in prompt['messages']:
                    if isinstance(msg, dict):
                        formatted_messages.append(msg)
                    elif hasattr(msg, 'to_dict'):
                        formatted_messages.append(msg.to_dict())
                    elif hasattr(msg, 'role') and hasattr(msg, 'content'):
                        formatted_messages.append({
                            'role': msg.role,
                            'content': msg.content
                        })
                
                # If system is in prompt, use it
                if 'system' in prompt and not system:
                    system = prompt['system']
            
            # If prompt is a string, use it as a single user message
            elif prompt and isinstance(prompt, str):
                formatted_messages.append({
                    'role': 'user',
                    'content': prompt
                })
            
            # Add system message if available (but not for Anthropic)
            system_content = system
            if system and 'anthropic' not in llm_client_type:
                # Check if there's already a system message
                has_system = any(msg.get('role') == 'system' for msg in formatted_messages)
                if not has_system:
                    formatted_messages.insert(0, {
                        'role': 'system',
                        'content': system
                    })
                    
            # Log how many messages we've prepared
            self.logger.debug(f"Prepared {len(formatted_messages)} messages for LLM, system message: {'present' if system else 'absent'}")
            
            # Make the request to the LLM client based on its specific API
            response = None
            await fresh_credential(self.llm_client)
            
            if 'anthropic' in llm_client_type:
                # For Anthropic, we need to:
                # 1. Extract any system messages from the formatted_messages
                # 2. Pass system content separately
                # 3. Filter metadata for Anthropic API
                
                # First check if there's a system message in formatted_messages and extract it
                anthropic_messages = []
                for msg in formatted_messages:
                    if msg.get('role') == 'system':
                        if not system_content:  # Only use if we don't already have system content
                            system_content = msg.get('content', '')
                    else:
                        anthropic_messages.append(msg)
                
                # Create a filtered version of metadata - Anthropic only allows specific metadata fields
                anthropic_metadata = None
                if metadata:
                    # Only pass through user_id which is allowed in Anthropic's metadata
                    anthropic_metadata = {}
                    if 'user_id' in metadata:
                        anthropic_metadata['user_id'] = metadata['user_id']
                        
                    if len(anthropic_metadata) == 0:
                        anthropic_metadata = None
                
                # Make the API call with system parameter separate from messages
                response = await self.llm_client.generate_response(
                    messages=anthropic_messages,  # Send messages WITHOUT system role
                    system=system_content,  # Pass system content separately
                    metadata=anthropic_metadata,  # Use filtered metadata
                    **llm_params
                )
                
            elif 'openai' in llm_client_type or 'llama' in llm_client_type:
                # For OpenAI/Llama just pass messages with system message included
                
                # For OpenAI specifically, make sure metadata is properly formatted
                # (OpenAI expects character to be a string, not an object)
                if 'openai' in llm_client_type and metadata:
                    openai_metadata = {}
                    
                    # Handle special case for 'character'
                    if 'character' in metadata and isinstance(metadata['character'], dict):
                        if 'name' in metadata['character']:
                            openai_metadata['character'] = metadata['character']['name']
                    
                    # Copy other simple values
                    for k, v in metadata.items():
                        if k != 'character' and isinstance(v, (str, int, float, bool)):
                            openai_metadata[k] = v
                    
                    # Check if we have any metadata to send
                    if openai_metadata:
                        # For OpenAI, we need to decide if we want to enable store
                        # Based on settings, we may choose to enable or disable metadata
                        use_metadata = True
                        # Get store setting from llm_params if available
                        store_enabled = llm_params.get('store', False)
                        
                        if use_metadata and not store_enabled:
                            # We could enable store here, but for now let's just log and skip metadata
                            self.logger.warning("OpenAI metadata provided but store not enabled, skipping metadata")
                            # Skip sending metadata
                            response = await self.llm_client.generate_response(
                                messages=formatted_messages,
                                **llm_params
                            )
                        else:
                            # Send metadata along with store=True
                            response = await self.llm_client.generate_response(
                                messages=formatted_messages,
                                metadata=openai_metadata,
                                store=True,
                                **llm_params
                            )
                    else:
                        # No metadata to send
                        response = await self.llm_client.generate_response(
                            messages=formatted_messages,
                            **llm_params
                        )
                else:
                    # For Llama or when no metadata present
                    response = await self.llm_client.generate_response(
                        messages=formatted_messages,
                        metadata=metadata,
                        **llm_params
                    )
                
            else:
                # Generic fallback - try to be robust for any client type
                # Pass both system and messages separately, and as a combined 'prompt'
                prompt_bundle = {
                    "messages": formatted_messages,
                    "system": system
                }
                
                response = await self.llm_client.generate_response(
                    messages=formatted_messages,
                    system=system,
                    prompt=prompt_bundle,
                    metadata=metadata,
                    **llm_params
                )
            
            # Log response details
            client_type = self.llm_client.__class__.__name__
            model_name = getattr(self.llm_client, 'model_type', 'unknown')
            
            # Try to extract token usage from response if available
            token_info = "Token usage unknown"
            try:
                if hasattr(response, 'usage'):
                    usage = response.usage
                    token_info = f"Tokens: {usage.prompt_tokens} prompt + {usage.completion_tokens} completion = {usage.total_tokens} total"
            except Exception as e:
                self.logger.debug(f"Could not extract token usage: {e}")
                
            # Log consolidated information
            self.logger.info(f"LLM Response | Model: {model_name} | Client: {client_type} | {token_info}")
            
            # Extract string content from response
            if isinstance(response, str):
                return response
            elif hasattr(response, 'content'):
                return response.content
            elif hasattr(response, 'choices') and len(response.choices) > 0:
                # OpenAI-style response
                return response.choices[0].message.content
            elif hasattr(response, 'text'):
                return response.text
            else:
                # Try to convert to string as last resort
                return str(response)
            
        except Exception as e:
            self.logger.error(f"Error generating response: {e}")
            raise

    def _validate_dependencies(self) -> None:
        """Validate dependencies."""
        if not self.llm_client:
            self.llm_available = False
            self.logger.warning(f"{self.name} agent initialized without LLM - some features will be disabled")
            return
        self.llm_available = True

