"""OpenAI LLM client implementation."""

import logging
from typing import Dict, Any, Optional, List, Union, AsyncGenerator, Tuple
import json
import os
import asyncio
import time
from openai import AsyncOpenAI # type: ignore 

from .llm_client import LLMClient
from .token_counter import count_messages_tokens
from core.config import BotConfig
from core.exceptions import LLMError, LLMConfigError, LLMConnectionError, ServiceError

class OpenAIClient(LLMClient):
    """OpenAI LLM client.
    
    All model configuration comes from model_registry (single source of truth).
    Deprecated fallback dicts removed Dec 13, 2025.
    """

    def __init__(self, config: BotConfig, name: str = "openai_client"):
        """Initialize the client."""
        super().__init__(config=config, name=name)
        self._client = None
        
        # Model and API configuration
        openai_config = config.get_llm_config()['openai']
        self.model_type = openai_config.get('model', 'gpt-5')
        self.api_key = openai_config.get('api_key')
        self.organization = openai_config.get('organization')
        self.base_url = openai_config.get('base_url')
        self.last_response = None  # Store the last response for token usage reporting
        
        # Get defaults from model_registry (single source of truth)
        from modules.llm.model_registry import get_model_config
        model_config = get_model_config(self.model_type)

        # Get model limits from registry
        if model_config:
            model_defaults = model_config.max_completion_tokens or 16384
        else:
            model_defaults = 16384  # Conservative fallback

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        # Set default parameters from config or use model-specific defaults
        self.temperature = openai_config.get('temperature', 0.7)
        self.max_tokens = openai_config.get('max_tokens', model_defaults)
        
        self.logger.debug(
            f"OpenAI client initialized: model={self.model_type}, "
            f"supports_vision={self.supports_vision}"
        )
        
    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError("OpenAI API key not provided")
    
    async def _setup_client(self) -> None:
        """Set up the OpenAI client."""
        try:
            # Configure client parameters
            client_kwargs = {
                "api_key": self.api_key,
            }
            
            # Add optional parameters if provided
            if self.organization:
                client_kwargs["organization"] = self.organization
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
                
            # Initialize client
            self._client = AsyncOpenAI(**client_kwargs)
            self.logger.debug("OpenAI client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up OpenAI client: {e}")
            
    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return
            
        try:
            # Validate API key
            self._validate_llm_config()
            
            # Setup client
            await self._setup_client()
            
            # Test connection (skipped for fast startup; validates lazily)
            if not self._skip_validate:
                await self._validate_connection()
            
            # Mark as initialized
            self._initialized = True
            
        except Exception as e:
            # Single error log with clear cause
            error_msg = f"Failed to initialize OpenAI client: {str(e)}"
            self.logger.error(error_msg)
            raise ServiceError(error_msg)
            
    async def _validate_connection(self) -> None:
        """Validate OpenAI connection."""
        try:
            # Use max_completion_tokens for newer models (gpt-5+) instead of max_tokens
            response = await self._client.chat.completions.create(
                model=self.model_type,
                messages=[{"role": "user", "content": "Test connection"}],
                max_completion_tokens=10
            )
            if not response or not response.choices:
                raise LLMConnectionError("No valid response from OpenAI API")
            self.logger.debug("OpenAI API connection validated successfully")
        except Exception as e:
            self.logger.error(f"Failed to validate OpenAI connection: {e}")
            raise ServiceError(f"Failed to validate OpenAI connection: {e}")
            
    async def _cleanup_client(self) -> None:
        """Clean up OpenAI client."""
        try:
            if self._client:
                # Close the underlying HTTP session if possible
                if hasattr(self._client, 'close'):
                    await self._client.close()
                # Set to None for garbage collection
                self._client = None
                self.logger.debug("OpenAI client resources released")
        except Exception as e:
            self.logger.error(f"Error cleaning up OpenAI client: {e}")
            
    async def _generate(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from OpenAI API."""
        start_time = time.time()
        success = False
        error_message = None
        
        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()
                
            # Format messages and validate roles
            openai_messages = []
            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')
                
                # Map roles
                if role in ['user', 'system', 'assistant', 'tool']:
                    openai_role = role
                elif role == 'ai':
                    openai_role = 'assistant'
                else:
                    self.logger.warning(f"Unknown role {role}, treating as user")
                    openai_role = 'user'
                    
                # Handle tool messages with tool_call_id
                message_dict = {
                    "role": openai_role,
                    "content": content
                }
                
                # Add tool_call_id for tool messages
                if role == 'tool' and 'tool_call_id' in msg:
                    message_dict["tool_call_id"] = msg["tool_call_id"]
                
                # Add tool_calls for assistant messages
                if role == 'assistant' and 'tool_calls' in msg:
                    message_dict["tool_calls"] = msg["tool_calls"]
                    
                openai_messages.append(message_dict)
                
            # Use provided parameters or default to instance attributes
            temp = temperature if temperature is not None else self.temperature

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(messages=openai_messages, max_tokens=max_tokens)

            # Log request details
            self.logger.debug(f"Sending request to OpenAI, model={self.model_type}, temp={temp}, max_tokens={max_tokens_value}")
            
            # Filter kwargs to only include supported parameters
            supported_params = {
                'model', 'messages', 'temperature', 'max_tokens', 
                'top_p', 'n', 'stop', 'presence_penalty', 'frequency_penalty',
                'logit_bias', 'user', 'seed', 'response_format'
            }
            
            # Special handling for metadata - it requires 'store' to be enabled
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_params}
            
            # If metadata is in kwargs, ensure store is enabled
            if 'metadata' in kwargs:
                # We should have store=True since we set it in generate_response
                if not kwargs.get('store', False):
                    self.logger.debug("Setting store=True since metadata is provided")
                filtered_kwargs['metadata'] = kwargs['metadata']
                filtered_kwargs['store'] = True
            
            # H1: SSOT decision for models that reject a custom temperature (o-series
            # reasoning models + gpt-5). Prefix-based, so o3/o4-mini are covered.
            from modules.llm.model_registry import openai_omits_temperature
            if openai_omits_temperature(self.model_type) and 'temperature' in filtered_kwargs:
                self.logger.info(f"Removing unsupported parameter 'temperature' for model {self.model_type}")
                filtered_kwargs.pop('temperature')

            # Log the final parameters being sent to OpenAI API for debugging
            log_params = {
                'model': self.model_type,
                'max_completion_tokens': max_tokens_value
            }
            if not (is_reasoning_model or is_temp_restricted):
                log_params['temperature'] = temp
            self.logger.debug(f"Final OpenAI API parameters: {log_params}")

            # Make API request with filtered parameters
            request_params = {
                'model': self.model_type,
                'messages': openai_messages,
                'max_completion_tokens': max_tokens_value,
            }

            # Only add temperature for models that support it
            if not (is_reasoning_model or is_temp_restricted) and temp is not None:
                request_params['temperature'] = temp
                
            # Add other filtered parameters
            request_params.update(filtered_kwargs)

            # P-3: stable cache bucket keyed on the (inline) system prompt, if any.
            _system_inline = next((m.get('content') for m in openai_messages if m.get('role') == 'system'), None)
            _cache_key = self._stable_prompt_cache_key(_system_inline)
            if _cache_key:
                request_params['prompt_cache_key'] = _cache_key

            # Make the actual API call
            self.last_response = await self._client.chat.completions.create(**request_params)
            
            # Mark as successful
            success = True
            
            # Process response
            response_text = ""
            if hasattr(self.last_response, 'choices') and self.last_response.choices:
                choice = self.last_response.choices[0]
                if hasattr(choice, 'message') and choice.message:
                    response_text = choice.message.content or ""
            
            # Capture telemetry with actual timing and usage data
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))
            
            return response_text
            
        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"OpenAI API error: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"OpenAI API error: {e}")
            
    async def generate_response(
        self,
        prompt: Optional[Union[str, Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """Generate a response from the LLM."""
        try:
            # Handle prompt formats
            if messages is not None:
                # Use messages directly if provided
                formatted_messages = messages
            elif isinstance(prompt, list) and all(isinstance(m, dict) for m in prompt):
                # Prompt is already a list of messages
                formatted_messages = prompt
            elif isinstance(prompt, dict) and 'messages' in prompt:
                # Extract messages from prompt dict
                formatted_messages = prompt['messages']
            elif isinstance(prompt, str):
                # Convert string prompt to message format
                formatted_messages = [{"role": "user", "content": prompt}]
            else:
                # Default to empty message
                formatted_messages = [{"role": "user", "content": "Hello"}]
            
            # Add system message if provided and not already present
            if system:
                has_system = any(msg.get('role') == 'system' for msg in formatted_messages)
                if not has_system:
                    formatted_messages.insert(0, {"role": "system", "content": system})
            
            # Process metadata for OpenAI
            # OpenAI allows metadata but with specific formatting requirements:
            # https://platform.openai.com/docs/api-reference/chat/create
            filtered_kwargs = kwargs.copy()
            
            # Ensure metadata is passed through to _generate
            if metadata:
                filtered_kwargs['metadata'] = metadata
            
            # Filter out metadata that OpenAI API doesn't support in the request
            # Store it in kwargs so _generate can access it but don't send to API
            api_metadata = None
            if metadata:
                # Extract what OpenAI supports vs. what we use internally
                api_metadata = {}
                internal_metadata = {}
                
                for key, value in metadata.items():
                    if key in ['user_id', 'session_id', 'agent_id']:
                        # These are for our internal tracking
                        internal_metadata[key] = value
                    else:
                        # These might be for OpenAI API
                        api_metadata[key] = value
                
                # Only include API metadata if it has valid content
                if api_metadata:
                    # OpenAI has strict requirements for metadata values
                    clean_api_metadata = {}
                    for key, value in api_metadata.items():
                        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 512:
                            clean_api_metadata[key] = value
                    
                    if clean_api_metadata:
                        filtered_kwargs['metadata'] = clean_api_metadata
                        filtered_kwargs['store'] = True  # Required when using metadata
                    # NOTE: do NOT overwrite filtered_kwargs['metadata'] with the raw
                    # internal metadata here — filtered_kwargs is sent to the OpenAI API,
                    # which rejects non-scalar/>512-char values and would receive
                    # unfiltered internal fields. The raw `metadata` dict stays in scope
                    # for local telemetry; only the sanitized copy goes to the wire.
            
            # H1: models that reject a custom temperature (o-series + gpt-5).
            from modules.llm.model_registry import openai_omits_temperature
            if openai_omits_temperature(self.model_type) and temperature is not None:
                self.logger.info(f"Ignoring temperature parameter for model {self.model_type}")
                temperature = None
            
            # Generate response
            response = await self._generate(
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **filtered_kwargs
            )
            
            return response
            
        except Exception as e:
            self.logger.error(f"Failed to generate response: {e}")
            raise ServiceError(f"Failed to generate response: {e}")
            
    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._initialized = False
        self.logger.info("OpenAI client cleaned up")

    def _validate_tool_call_pairs(self, messages: List[Dict[str, Any]]) -> None:
        """Validate that all tool_calls have matching tool responses.

        This preflight check prevents sending invalid sequences to OpenAI that would
        cause 400 errors like "assistant message with 'tool_calls' must be followed
        by tool messages before another assistant message".

        Args:
            messages: List of formatted messages to validate

        Raises:
            LLMError: If validation fails with details about the issue
        """
        from core.exceptions import LLMError

        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
                expected_ids = set()

                # Extract all tool call IDs from this assistant message
                for tc in tool_calls:
                    tc_id = tc.get("id")
                    if tc_id:
                        expected_ids.add(tc_id)

                if not expected_ids:
                    continue

                # Check that the next N messages are tool responses with matching IDs
                found_ids = set()
                j = i + 1

                # Scan ahead for tool messages
                while j < len(messages) and len(found_ids) < len(expected_ids):
                    next_msg = messages[j]

                    # If we hit another role before finding all tool messages, that's an error
                    if next_msg.get("role") != "tool":
                        missing = expected_ids - found_ids
                        if missing:
                            self.logger.error(
                                f"Invalid sequence at message {i}: Assistant message with {len(expected_ids)} "
                                f"tool_calls is missing tool responses for IDs: {missing}"
                            )
                            raise LLMError(
                                f"Invalid message sequence: Assistant message at index {i} with "
                                f"tool_calls {list(expected_ids)} is missing tool responses for "
                                f"{list(missing)}. Found a {next_msg.get('role')} message instead."
                            )
                        break

                    # Track the tool response ID
                    tool_call_id = next_msg.get("tool_call_id")
                    if tool_call_id:
                        if tool_call_id in expected_ids:
                            found_ids.add(tool_call_id)
                        else:
                            self.logger.warning(
                                f"Tool message at index {j} has unexpected tool_call_id {tool_call_id}"
                            )

                    j += 1

                # Check if all expected IDs were found
                missing = expected_ids - found_ids
                if missing:
                    self.logger.error(
                        f"Assistant message at index {i} has unmatched tool_calls: {missing}"
                    )
                    raise LLMError(
                        f"Invalid message sequence: Assistant message at index {i} has "
                        f"{len(missing)} unmatched tool_calls: {list(missing)}. "
                        "Each tool_call must have a corresponding tool response message."
                    )

    def _stable_prompt_cache_key(self, system: Optional[str]) -> Optional[str]:
        """Stable per-session key for OpenAI prompt caching (R3 / P-3).

        OpenAI caches identical request prefixes server-side automatically, but
        `prompt_cache_key` lets us route same-prefix requests to the same cache
        bucket, improving hit-rate for our stable system prompt + tool schemas.
        The system prompt is built once per session and is stable across steps, so
        hashing it yields one key per session. Returns None when there's no system
        prompt to key on (let OpenAI fall back to automatic prefix matching).
        """
        if not system:
            return None
        import hashlib
        digest = hashlib.sha256(f"{self.model_type}|{system}".encode("utf-8")).hexdigest()[:32]
        return f"rob-{digest}"

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,  # ✅ ADD THIS
        temperature: Optional[float] = None,  # ✅ ADD THIS
        max_tokens: Optional[int] = None,  # ✅ ADD THIS
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Union[str, Tuple[str, List[Dict[str, Any]]]]:
        """Implement OpenAI-specific tool-based generation.

        SIMPLIFIED: Only performs wire-format conversion for OpenAI API.
        All validation and repair is handled by tool_call_builder.repair_and_normalize()
        before messages reach this point.
        """
        try:
            # WIRE-FORMAT ONLY: Convert tool calls to OpenAI nested format
            self.logger.debug(f"Starting message formatting for OpenAI")
            formatted_messages = []

            for msg in messages:
                formatted_msg = msg.copy()

                # Only convert wire format for assistant messages with tool_calls
                if msg["role"] == "assistant" and "tool_calls" in msg:
                    # Ensure tool calls have proper nested format for OpenAI API
                    tool_calls = []
                    for tool_call in msg.get("tool_calls", []):
                        # Convert to OpenAI wire format if needed
                        if not isinstance(tool_call.get("function"), dict):
                            # Convert flat format to nested OpenAI format
                            if "name" in tool_call and "args" in tool_call:
                                tool_calls.append({
                                    "id": tool_call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tool_call["name"],
                                        "arguments": json.dumps(tool_call["args"]) if not isinstance(tool_call.get("args"), str) else tool_call["args"]
                                    }
                                })
                            else:
                                # Already in correct format or malformed
                                tool_calls.append(tool_call)
                        else:
                            # Already in OpenAI format
                            tool_calls.append(tool_call)

                    formatted_msg["tool_calls"] = tool_calls

                formatted_messages.append(formatted_msg)
                self.logger.debug(f"Formatted message {len(formatted_messages)}: role={formatted_msg.get('role')}, has_tool_calls={bool(formatted_msg.get('tool_calls'))}")

            # PREFLIGHT VALIDATION: Ensure tool calls have matching responses
            self._validate_tool_call_pairs(formatted_messages)

            # ✅ FIX: Use explicit parameters instead of burying in kwargs
            # Use provided parameters or instance defaults
            temp = temperature if temperature is not None else self.temperature
            max_tokens_value = max_tokens if max_tokens is not None else self.max_tokens

            # Handle system message - OpenAI expects it in the messages list
            if system:
                # Check if system message already in messages
                has_system = any(msg.get("role") == "system" for msg in formatted_messages)
                if not has_system:
                    formatted_messages.insert(0, {"role": "system", "content": system})
                    self.logger.debug("Injected system message into messages list")

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(messages=formatted_messages, max_tokens=max_tokens_value)

            self.logger.info(f"OpenAI API request with tools: model={self.model_type}, tools={len(tools) if tools else 0}, max_tokens={max_tokens_value}, has_system={bool(system)}")

            # Filter kwargs to supported parameters
            supported_params = {
                'tool_choice', 'top_p', 'presence_penalty', 'frequency_penalty',
                'logit_bias', 'user', 'seed', 'response_format', 'stop'
            }
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_params}

            # Build request parameters
            request_params = {
                'model': self.model_type,
                'messages': formatted_messages,
                'tools': tools,
                'tool_choice': "auto" if tools else None,
                'max_completion_tokens': max_tokens_value,
            }

            # Add temperature only for models that support it (H1: o-series + gpt-5 reject it).
            from modules.llm.model_registry import openai_omits_temperature
            if not openai_omits_temperature(self.model_type):
                request_params['temperature'] = temp

            request_params.update(filtered_kwargs)

            # P-3: route same-prefix requests to a stable cache bucket.
            cache_key = self._stable_prompt_cache_key(system)
            if cache_key:
                request_params['prompt_cache_key'] = cache_key

            # UP-07: per-model reasoning_effort from the registry (gated, default OFF).
            # Inert unless a model entry sets reasoning_effort AND the gate is on.
            if 'reasoning_effort' not in request_params:
                try:
                    from modules.llm.model_registry import thinking_config_enabled, get_thinking_config
                    if thinking_config_enabled():
                        effort = get_thinking_config(self.model_type).get('reasoning_effort')
                        if effort:
                            request_params['reasoning_effort'] = effort
                except Exception:
                    pass

            # Create completion request with tools
            self.logger.debug(f"Calling OpenAI API with {len(formatted_messages)} messages")
            response = await self._client.chat.completions.create(**request_params)
            self.last_response = response  # ✅ Store for telemetry
            
            # Handle response
            message = response.choices[0].message

            # Extract usage data from response
            usage_data = self._extract_usage_data()

            # If there are tool calls, return tuple with content, tool calls, and usage
            if hasattr(message, 'tool_calls') and message.tool_calls:
                content = message.content

                # OpenAI commonly returns tool calls without content in native tool mode
                if not content or not content.strip():
                    self.logger.debug(
                        f"OpenAI returned tool calls without content (expected in native tool mode). "
                        f"Tool calls: {[tc.function.name for tc in message.tool_calls[:3]]}{'...' if len(message.tool_calls) > 3 else ''}"
                    )
                    content = ""

                tool_calls_list = [
                    {
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    }
                    for tool_call in message.tool_calls
                ]

                self.logger.info(f"[generate_with_tools] Returning 3-tuple with {len(tool_calls_list)} tool calls + usage")
                return (content, tool_calls_list, usage_data)

            # No tool calls - return consistent 3-tuple format with empty tool calls and usage data
            # CRITICAL: This should NOT happen in normal operation - log as warning
            self.logger.warning(f"[DEBUG] OpenAI response has NO tool calls - LLM failed to call functions (expected in native tool mode)")
            return (message.content or "", [], usage_data)
            
        except Exception as e:
            self.logger.error(f"OpenAI tool-based generation failed: {str(e)}")
            raise LLMError(f"OpenAI generation failed: {str(e)}")

    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Union[Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]], str]:
        """Generate response with tool-calling support for agents.

        ⚠️ CRITICAL CONTRACT: This method MUST extract system, temperature, and max_tokens
        from kwargs and pass them explicitly to _generate_with_tools. These parameters
        contain the system prompt with brain state instructions.

        This method bridges the adapter contract to enable native tool-calling
        for OpenAI. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in provider format
            **kwargs: Additional generation parameters
                - system: System prompt (CRITICAL for brain state instructions)
                - temperature: Temperature setting
                - max_tokens: Max completion tokens
                - metadata: Request metadata

        Returns:
            Either:
            - Tuple of (content, tool_calls, usage_data) when tool calls are present
              usage_data dict contains: prompt_tokens, completion_tokens, total_tokens
            - Just the content string when no tool calls

        Raises:
            LLMError: If generation fails
        """
        try:
            self.logger.info(f"[generate_agent_response] Called with {len(messages)} messages and {len(tools) if tools else 0} tools")

            # ✅ FIX: Extract system message and parameters from kwargs (match Anthropic pattern)
            system = kwargs.pop('system', None)
            temperature = kwargs.pop('temperature', None)
            max_tokens = kwargs.pop('max_tokens', None)

            # Call the existing _generate_with_tools method with explicit parameters
            result = await self._generate_with_tools(
                messages=messages,
                tools=tools,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            # Log the result type for debugging
            if isinstance(result, tuple):
                if len(result) == 3:
                    content, tool_calls, usage = result
                    self.logger.info(f"[generate_agent_response] Returning 3-tuple with {len(tool_calls)} tool calls + usage")
                else:
                    # Backward compatibility - shouldn't happen with new code
                    content, tool_calls = result
                    self.logger.warning(f"[generate_agent_response] Got 2-tuple (old format) with {len(tool_calls)} tool calls")
            else:
                self.logger.info(f"[generate_agent_response] Returning content only (no tool calls)")

            return result

        except LLMError:
            # Re-raise LLM errors as-is
            raise
        except Exception as e:
            # Wrap other exceptions using core.exceptions pattern
            self.logger.error(f"generate_agent_response failed: {str(e)}", exc_info=True)
            raise LLMError(f"Failed to generate agent response: {str(e)}")

    def _validate_tool_message_sequence(
        self,
        messages: List[Dict[str, Any]]
    ) -> bool:
        """Validate that tool messages properly follow tool calls."""
        tool_call_ids = set()

        for msg in messages:
            if msg["role"] == "assistant" and "tool_calls" in msg:
                for tool_call in msg.get("tool_calls", []):
                    tool_call_ids.add(tool_call["id"])
            elif msg["role"] == "tool":
                if "tool_call_id" not in msg:
                    return False
                if msg["tool_call_id"] not in tool_call_ids:
                    return False
                tool_call_ids.remove(msg["tool_call_id"])

        return True

    async def _make_validation_request(self) -> Any:
        """Make minimal test request to OpenAI."""
        return await self._client.chat.completions.create(
            model=self.model_type,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )

    def _check_validation_response(self, response: Any) -> None:
        """Validate OpenAI response."""
        if not response or not response.choices:
            raise ValueError("Invalid response from OpenAI")

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from last_response.

        Returns dict with prompt_tokens, completion_tokens, total_tokens, cached_tokens.
        Returns None values if usage data not available.

        Note: OpenAI may return additional fields:
        - prompt_tokens_details.cached_tokens (for prompt caching)
        - completion_tokens_details.reasoning_tokens (for o1/o3 models)
        """
        if not self.last_response:
            self.logger.warning("No last_response available for usage extraction")
            return {
                'prompt_tokens': None,
                'completion_tokens': None,
                'total_tokens': None,
                'cached_tokens': None
            }

        if not hasattr(self.last_response, 'usage') or not self.last_response.usage:
            self.logger.warning(f"No usage data in response for model {self.model_type}")
            return {
                'prompt_tokens': None,
                'completion_tokens': None,
                'total_tokens': None,
                'cached_tokens': None
            }

        usage = self.last_response.usage

        # Extract basic token counts
        prompt_tokens = getattr(usage, 'prompt_tokens', None)
        completion_tokens = getattr(usage, 'completion_tokens', None)
        total_tokens = getattr(usage, 'total_tokens', None)

        # Extract cached tokens if present (for prompt caching)
        cached_tokens = None
        if hasattr(usage, 'prompt_tokens_details'):
            details = usage.prompt_tokens_details
            if details and hasattr(details, 'cached_tokens'):
                cached_tokens = getattr(details, 'cached_tokens', None)
                if cached_tokens:
                    self.logger.debug(f"Found cached_tokens: {cached_tokens}")

        # Extract reasoning tokens if present (o1/o3 models)
        # These are already included in completion_tokens by OpenAI
        if hasattr(usage, 'completion_tokens_details'):
            details = usage.completion_tokens_details
            if details and hasattr(details, 'reasoning_tokens'):
                reasoning_tokens = getattr(details, 'reasoning_tokens', None)
                if reasoning_tokens:
                    self.logger.debug(f"Found reasoning_tokens: {reasoning_tokens} (included in completion_tokens)")

        usage_data = {
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
            'cached_tokens': cached_tokens or 0  # Default to 0 if not present
        }

        self.logger.debug(f"Extracted usage: {usage_data}")
        return usage_data
