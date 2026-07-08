"""Anthropic Claude API Client."""

import json
import logging
import asyncio
import time
from typing import List, Dict, Any, Optional, Union, Tuple

import anthropic
from anthropic.types import ContentBlock, MessageParam, Message

from modules.llm.llm_client import LLMClient
from modules.llm.token_counter import count_messages_tokens
from core.exceptions import LLMError, LLMConfigError, LLMRateLimitError, LLMAuthenticationError, LLMContextLengthError, LLMInvalidRequestError, LLMConnectionError, ServiceError
from core.config import BotConfig
import os

# Import default model from registry
from modules.llm.llm_client_registry import get_default_model


def _clamp_thinking(model_cap, budget, current_max_tokens):
    """H4: return (budget, max_tokens) valid for Anthropic extended thinking.

    Anthropic requires ``max_tokens > thinking.budget_tokens`` AND
    ``max_tokens <= the model's real completion cap``. Registry entries set
    budget == cap, so the old ``max_tokens = budget + 4096`` overran the cap and 400'd.
    Shrink the budget to leave >=4096 output room under the cap; if the cap is too
    small to fit any thinking, return (None, ...) so the caller disables thinking.
    """
    if not budget or budget <= 0:
        return None, current_max_tokens
    if model_cap and model_cap > 0:
        max_budget = model_cap - 4096
        if max_budget < 1024:
            # Cap too small to fit thinking + output room — disable thinking.
            return None, min(current_max_tokens, model_cap)
        if budget > max_budget:
            budget = max_budget
        max_tokens = current_max_tokens
        if max_tokens <= budget:
            max_tokens = budget + 4096
        max_tokens = min(max_tokens, model_cap)
        return budget, max_tokens
    # No cap known — preserve legacy behaviour (bump above budget).
    max_tokens = current_max_tokens
    if max_tokens <= budget:
        max_tokens = budget + 4096
    return budget, max_tokens


def _build_cached_system_param(system):
    """Build Anthropic ``system`` as content blocks with a prompt-cache breakpoint.

    Flow-efficiency D4-a: the system prompt (and the tool definitions that precede
    it in the request) are stable across steps within a session, so marking the
    LAST system block with ``cache_control: ephemeral`` lets Anthropic serve the
    whole tools+system prefix from cache (~10x cheaper on repeated input).

    Args:
        system: the system prompt as a ``str`` or a list of content-block dicts.

    Returns:
        A list of content-block dicts (last one carrying ``cache_control`` when
        caching is enabled), or ``None`` if ``system`` is falsy.

    Caching is on by default; set ``ANTHROPIC_PROMPT_CACHE=0`` to disable.
    Does not mutate the input list.
    """
    if not system:
        return None

    if isinstance(system, list):
        blocks = [dict(b) for b in system]  # shallow copy so we never mutate caller's list
    else:
        blocks = [{"type": "text", "text": system}]

    from modules.llm.cache_hints import prompt_cache_enabled
    if prompt_cache_enabled() and blocks:
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks


def _apply_conversation_cache(messages, n: int = 3):
    """Mark the last ``n`` conversation messages with ``cache_control: ephemeral``.

    Flow-efficiency B1 (Reference ``system_and_3`` parity, see
    docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9): Anthropic allows up to **4**
    cache breakpoints per request. ``_build_cached_system_param`` already spends 1 on
    the system block; this spends the remaining 3 on the tail of the *conversation*,
    so the growing message prefix is served from cache instead of re-paying full input
    cost every turn.

    Does not mutate the caller's list. No-op (returns input unchanged) when
    ``ANTHROPIC_PROMPT_CACHE`` is disabled or ``messages`` is empty.
    """
    from modules.llm.cache_hints import prompt_cache_enabled
    if not prompt_cache_enabled() or not messages:
        return messages

    out = [dict(m) for m in messages]
    for m in out[-n:]:
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            # copy the last block so we don't mutate the shared dict, then mark it
            content = [dict(b) if isinstance(b, dict) else b for b in content]
            if isinstance(content[-1], dict):
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
            m["content"] = content
    return out


class AnthropicClient(LLMClient):
    """Anthropic Claude API client.
    
    All model configuration comes from model_registry (single source of truth).
    Deprecated fallback dicts removed Dec 13, 2025.
    """

    # UP-07: the dead EXTENDED_THINKING_MODELS dict was removed — its budgets now live in
    # model_registry (thinking_budget_tokens) and drive a real `thinking` block below.

    def __init__(self, config: BotConfig, name: str = "anthropic_client"):
        """Initialize the client."""
        super().__init__(config=config, name=name)
        self._client = None
        
        # Model and API configuration
        self.model_type = config.get_llm_config()['anthropic'].get('model', get_default_model('anthropic'))
        self.api_key = config.get_llm_config()['anthropic']['api_key']
        self.last_response = None  # Store the last response for token usage reporting
        
        # Get defaults from model_registry (single source of truth)
        from modules.llm.model_registry import get_model_config
        model_config = get_model_config(self.model_type)
        
        if model_config:
            self.max_tokens = model_config.max_completion_tokens or 8192
        else:
            self.max_tokens = 8192  # Conservative fallback

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = 0.7  # Default temperature

        self.logger.debug(
            f"Anthropic client initialized: model={self.model_type}, "
            f"supports_vision={self.supports_vision}"
        )

    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError("Anthropic API key not provided")

    def _profile_base_url(self) -> Optional[str]:
        """Read base_url from the Anthropic ProviderProfile (None => SDK default).

        Delegates to the base ``_resolve_profile_base_url`` helper so that a
        self-hosted gateway / proxy is configured in one place (profiles.py).
        """
        return self._resolve_profile_base_url("anthropic")

    async def _setup_client(self) -> None:
        """Set up the Anthropic client."""
        try:
            kwargs = {"api_key": self.api_key}
            base_url = self._profile_base_url()
            if base_url:
                kwargs["base_url"] = base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
            self.logger.debug("Anthropic client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up Anthropic client: {e}")
        
    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return
        try:
            # Validate configuration
            self._validate_llm_config()
            
            # Initialize client with API key
            await self._setup_client()

            # Validate connection (skipped for fast startup; clients validate
            # lazily on first real call — matches the base client and gemini)
            if not self._skip_validate:
                await self._validate_connection()

            self._initialized = True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Anthropic client: {e}")
            raise ServiceError(f"Failed to initialize Anthropic client: {e}")

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Generate response with native tool/function calling support.

        Returns:
            Tuple of (content, tool_calls) where tool_calls is a list of tool call dicts
        """
        start_time = time.time()
        success = False
        error_message = None

        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()

            # Convert messages to Anthropic format
            anthropic_messages = []
            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')

                # Skip system messages as they're handled separately, BUT recover
                # the system prompt from the message list when no explicit `system`
                # kwarg was supplied — the agent builds the real system prompt in the
                # message list, not via the kwarg. Without this the entire system
                # prompt (persona/brain-state format/security/tool guidance) is
                # silently dropped on the Anthropic native tool path.
                if role == 'system':
                    if system is None and isinstance(content, str) and content:
                        system = content
                    continue

                # Map roles
                if role == 'user':
                    anthropic_role = 'user'
                elif role in ['assistant', 'ai']:
                    anthropic_role = 'assistant'
                elif role == 'tool':
                    # Handle tool result messages
                    anthropic_role = 'user'
                    # Anthropic expects tool results in a specific format
                    tool_call_id = msg.get('tool_call_id', '')
                    content = [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content
                    }]
                else:
                    self.logger.warning(f"Unknown role {role}, treating as user")
                    anthropic_role = 'user'

                # Handle tool calls in assistant messages
                if role == 'assistant' and 'tool_calls' in msg:
                    # Convert tool calls to Anthropic format
                    content_blocks = []
                    if content:
                        content_blocks.append({
                            "type": "text",
                            "text": content
                        })

                    for tool_call in msg['tool_calls']:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_call.get('id', ''),
                            "name": tool_call.get('function', {}).get('name', ''),
                            "input": json.loads(tool_call.get('function', {}).get('arguments', '{}'))
                        })

                    anthropic_messages.append({
                        "role": anthropic_role,
                        "content": content_blocks
                    })
                else:
                    # Regular message
                    anthropic_messages.append({
                        "role": anthropic_role,
                        "content": content
                    })

            # Use provided parameters or defaults
            temp = temperature if temperature is not None else self.temperature

            # Estimate input tokens (messages + system, Anthropic sends them separately)
            estimated_input_tokens = count_messages_tokens(messages, self.model_type)
            if system:
                estimated_input_tokens += count_messages_tokens([{"role": "system", "content": system}], self.model_type)

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=estimated_input_tokens,
            )

            # Log request
            self.logger.info(f"Anthropic API request with tools: model={self.model_type}, tools={len(tools)}, max_tokens={max_tokens_value}")

            # Filter kwargs - CRITICAL: Exclude 'system' to prevent override
            supported_params = {
                'model', 'messages', 'temperature', 'max_tokens',
                'tools', 'tool_choice', 'metadata', 'top_p', 'top_k', 'stop_sequences'
            }
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_params}

            # Make API request with tools
            # Convert system to list form + add prompt-cache breakpoint (D4-a).
            system_param = _build_cached_system_param(system)

            # Build API call params - only include system if not None
            api_params = {
                'model': self.model_type,
                'messages': anthropic_messages,
                'temperature': temp,
                'max_tokens': max_tokens_value,
                'tools': tools,
                'tool_choice': kwargs.get('tool_choice', {'type': 'auto'}),
                **filtered_kwargs
            }
            if system_param is not None:
                api_params['system'] = system_param
            # B1: cache the conversation prefix (system + last 3 msgs = 4 breakpoints).
            api_params['messages'] = _apply_conversation_cache(api_params['messages'])

            # UP-07: extended-thinking block from the registry budget (gated, default OFF).
            # Anthropic requires max_tokens > budget_tokens and temperature == 1 when
            # thinking is enabled. Per-call override via kwargs['thinking'] wins.
            from modules.llm.model_registry import (
                thinking_config_enabled, get_thinking_config, get_model_config,
            )
            # P1-9: extended thinking + tool calls requires replaying the assistant's
            # thinking block(s) (with their signature) ahead of tool_use on the NEXT
            # request; this client discards thinking blocks (see response processing
            # below) and never replays them, so the follow-up tool_result request 400s
            # at step 2 of a tool loop. Until block-replay lands, REFUSE to enable
            # thinking when tools are present (the agent's primary path is a tool loop).
            # A forced non-auto tool_choice is likewise invalid with thinking — moot
            # here since we never combine them. One-time WARN so it's visible.
            if 'thinking' not in api_params and thinking_config_enabled():
                if tools:
                    if not getattr(self, '_thinking_tools_warned', False):
                        self.logger.warning(
                            "extended thinking is disabled for tool-calling requests in "
                            "this build (thinking blocks are not yet replayed, which would "
                            "400 the follow-up tool_result request). Set THINKING_CONFIG_"
                            "ENABLED=off to silence, or use thinking only on no-tool calls."
                        )
                        self._thinking_tools_warned = True
                else:
                    tcfg = get_thinking_config(self.model_type)
                    budget = tcfg.get('budget_tokens')
                    if budget:
                        # H4: clamp so max_tokens > budget AND max_tokens <= the model cap
                        # (registry entries set budget == cap, which overran the cap -> 400).
                        _cfg = get_model_config(self.model_type)
                        model_cap = getattr(_cfg, 'max_completion_tokens', None) if _cfg else None
                        budget, max_tokens_value = _clamp_thinking(model_cap, budget, max_tokens_value)
                        if budget:
                            api_params['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
                            api_params['temperature'] = 1  # required by the API when thinking is on
                            api_params['max_tokens'] = max_tokens_value

            # FIXED: Use streaming for high max_tokens to avoid Anthropic SDK error
            # "Streaming is required for operations that may take longer than 10 minutes"
            # This happens when max_tokens is high (e.g., 65536). A thinking budget raises
            # max_tokens above 8192, so this path is always taken when thinking is on.
            use_streaming = max_tokens_value > 8192

            response_text = ""
            tool_calls = []

            if use_streaming:
                self.logger.debug(f"Using streaming for tool call (max_tokens={max_tokens_value})")
                # Use streaming to avoid timeout error
                async with self._client.messages.stream(**api_params) as stream:
                    self.last_response = await stream.get_final_message()
            else:
                self.last_response = await self._client.messages.create(**api_params)

            success = True

            # Process response - extract both content and tool calls
            if hasattr(self.last_response, 'content') and self.last_response.content:
                for block in self.last_response.content:
                    if hasattr(block, 'type'):
                        if block.type == 'text' and hasattr(block, 'text'):
                            response_text += block.text + " "
                        elif block.type == 'tool_use':
                            # Convert Anthropic tool use to standard format
                            tool_calls.append({
                                'id': block.id,
                                'type': 'function',
                                'function': {
                                    'name': block.name,
                                    'arguments': json.dumps(block.input) if isinstance(block.input, dict) else str(block.input)
                                }
                            })

            # Capture telemetry
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))

            # Extract usage data
            usage_data = self._extract_usage_data()

            return response_text.strip(), tool_calls, usage_data

        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Anthropic tool calling error: {e}")

            # Capture telemetry
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))

            # Fall back to non-tool generation
            self.logger.warning(f"Falling back to non-tool generation due to error: {e}")
            response = await self._generate(messages, system, temperature, max_tokens, **kwargs)
            # Canonical 3-tuple: usage is unavailable on this error path; use a zero/null dict
            # consistent with _extract_usage_data()'s no-response shape.
            fallback_usage: Dict[str, Optional[int]] = {
                'prompt_tokens': None,
                'completion_tokens': None,
                'total_tokens': None,
            }
            return response, [], fallback_usage

    # ✅ REFERENCE IMPLEMENTATION FOR ALL PROVIDERS
    # This method demonstrates the correct pattern:
    # 1. Extract system/temperature/max_tokens from kwargs
    # 2. Pass them explicitly to _generate_with_tools
    # 3. _generate_with_tools uses them in API call
    # OpenAI and Gemini should match this pattern.
    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]]:
        """Generate response with tool-calling support for agents.

        This method bridges the adapter contract to enable native tool-calling
        for Anthropic. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in provider format
            **kwargs: Additional generation parameters (including system, temperature, etc.)

        Returns:
            3-tuple (content, tool_calls, usage_data). tool_calls may be empty list when
            the model produced no tool calls. usage_data dict contains:
            prompt_tokens, completion_tokens, total_tokens.

        Raises:
            LLMError: If generation fails
        """
        try:
            self.logger.info(f"[generate_agent_response] Called with {len(messages)} messages and {len(tools) if tools else 0} tools")

            # Extract system message if present in kwargs
            system = kwargs.pop('system', None)
            temperature = kwargs.pop('temperature', None)
            max_tokens = kwargs.pop('max_tokens', None)

            # Call the existing _generate_with_tools method
            result = await self._generate_with_tools(
                messages=messages,
                tools=tools,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            # _generate_with_tools ALWAYS returns a 3-tuple (content, tool_calls, usage).
            # Unpack directly; a non-3-tuple here is a programming error.
            content, tool_calls, usage = result
            self.logger.info(f"[generate_agent_response] Returning 3-tuple with {len(tool_calls)} tool calls + usage")

            return result

        except LLMError:
            # Re-raise LLM errors as-is
            raise
        except Exception as e:
            # Wrap other exceptions using core.exceptions pattern
            self.logger.error(f"generate_agent_response failed: {str(e)}", exc_info=True)
            raise LLMError(f"Failed to generate agent response: {str(e)}")

    async def _generate(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from Claude API."""
        start_time = time.time()
        success = False
        error_message = None
        
        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()
                
            # Convert messages to Anthropic format, skipping system
            anthropic_messages = []
            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')
                
                # Skip system messages as they're handled separately in Claude API,
                # BUT recover the in-list system prompt when no explicit `system`
                # kwarg was supplied (see _generate_with_tools for rationale).
                if role == 'system':
                    if system is None and isinstance(content, str) and content:
                        system = content
                    continue

                # Map roles
                if role == 'user':
                    anthropic_role = 'user'
                elif role in ['assistant', 'ai']:
                    anthropic_role = 'assistant'
                else:
                    self.logger.warning(f"Unknown role {role}, treating as user")
                    anthropic_role = 'user'
                    
                anthropic_messages.append({
                    "role": anthropic_role,
                    "content": content
                })
                
            # Use provided parameters or default to instance attributes
            temp = temperature if temperature is not None else self.temperature

            # Estimate input tokens (messages + system, Anthropic sends them separately)
            estimated_input_tokens = count_messages_tokens(messages, self.model_type)
            if system:
                # Add system message token estimate
                estimated_input_tokens += count_messages_tokens([{"role": "system", "content": system}], self.model_type)

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=estimated_input_tokens,
            )

            # Log request details with proper values being used
            self.logger.info(f"Anthropic API request: model={self.model_type}, temperature={temp}, max_tokens={max_tokens_value}, est_input_tokens={estimated_input_tokens}")
            
            # Filter kwargs to only include supported parameters
            # CRITICAL: Exclude 'system' from kwargs to prevent override of system_param
            supported_params = {
                'model', 'messages', 'temperature', 'max_tokens', 
                'metadata', 'top_p', 'top_k', 'stop_sequences', 'stream'
            }
            
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_params}

            # Make API request
            # Convert system to list form + add prompt-cache breakpoint (D4-a).
            system_param = _build_cached_system_param(system)

            # Build API call params - only include system if not None
            api_params = {
                'model': self.model_type,
                'messages': anthropic_messages,
                'temperature': temp,
                'max_tokens': max_tokens_value,
                **filtered_kwargs
            }
            if system_param is not None:
                api_params['system'] = system_param
            # B1: cache the conversation prefix (system + last 3 msgs = 4 breakpoints).
            api_params['messages'] = _apply_conversation_cache(api_params['messages'])

            # FIXED: Use streaming for high max_tokens to avoid Anthropic SDK error
            # "Streaming is required for operations that may take longer than 10 minutes"
            use_streaming = max_tokens_value > 8192

            if use_streaming:
                self.logger.debug(f"Using streaming for _generate (max_tokens={max_tokens_value})")
                async with self._client.messages.stream(**api_params) as stream:
                    self.last_response = await stream.get_final_message()
            else:
                self.last_response = await self._client.messages.create(**api_params)
            
            # Mark as successful
            success = True
            
            # Process response
            response_text = ""
            if hasattr(self.last_response, 'content') and self.last_response.content:
                text_blocks = [
                    block.text for block in self.last_response.content 
                    if hasattr(block, 'text') and block.text
                ]
                response_text = " ".join(text_blocks)
            
            # Capture telemetry with actual timing and usage data
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))
                
            return response_text
            
        except anthropic.APIError as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Anthropic API error: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"Anthropic API error: {e}")
        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Error generating response from Anthropic: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"Error generating response from Anthropic: {e}")

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
                # Extract messages and system from prompt dict
                formatted_messages = prompt['messages']
                if 'system' in prompt and not system:
                    system = prompt['system']
            elif isinstance(prompt, str):
                # Convert string prompt to message format
                formatted_messages = [{"role": "user", "content": prompt}]
            else:
                # Default to empty message
                formatted_messages = [{"role": "user", "content": "Hello"}]
            
            # Anthropic doesn't support all metadata types
            # Filter to what we can use
            filtered_kwargs = kwargs.copy()
            
            # Ensure metadata is passed through to _generate for telemetry
            if metadata:
                filtered_kwargs['metadata'] = metadata
                
                # Extract Anthropic-supported metadata
                anthropic_metadata = {}
                for key, value in metadata.items():
                    # Anthropic supports user_id in metadata
                    if key == 'user_id' and isinstance(value, str):
                        anthropic_metadata[key] = value
                
                # Only include if we have valid metadata for Anthropic
                if anthropic_metadata:
                    filtered_kwargs.update(anthropic_metadata)
            
            # Generate response
            response = await self._generate(
                messages=formatted_messages,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                **filtered_kwargs
            )
            
            return response
            
        except Exception as e:
            self.logger.error(f"Failed to generate response: {e}")
            raise ServiceError(f"Failed to generate response: {e}")

    async def _validate_connection(self) -> None:
        """Validate connection to Anthropic."""
        try:
            # Simple test completion
            response = await self._client.messages.create(
                model=self.model_type,
                max_tokens=10,
                messages=[{"role": "user", "content": "Test connection"}]
            )
            if not response:
                raise LLMConnectionError("No response from Anthropic API")
            self.logger.debug("Anthropic API connection validated successfully")
        except Exception as e:
            self.logger.error(f"Failed to validate Anthropic connection: {e}")
            raise ServiceError(f"Failed to validate Anthropic connection: {e}")

    async def _cleanup_client(self) -> None:
        """Clean up Anthropic client."""
        try:
            if hasattr(self, '_client'):
                # Just set it to None for garbage collection
                self._client = None
                self.logger.debug("Anthropic client resources released")
        except Exception as e:
            self.logger.error(f"Error cleaning up Anthropic client: {e}")

    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._client = None
        self._initialized = False
        self.logger.info("Anthropic client cleaned up")

    def _format_messages(self, prompt: Union[str, List[Dict[str, Any]], Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Format messages for Anthropic API."""
        try:
            if isinstance(prompt, str):
                return [{
                    "role": "user",
                    "content": prompt
                }]
            elif isinstance(prompt, list):
                return [
                    {
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")
                    }
                    for msg in prompt
                    if msg.get("content") and msg.get("role") != "system"  # Skip system and empty messages
                ]
            elif isinstance(prompt, dict):
                messages = []
                
                # Handle messages list
                if "messages" in prompt:
                    return [
                        {
                            "role": msg.get("role", "user"),
                            "content": msg.get("content", "")
                        }
                        for msg in prompt["messages"]
                        if msg.get("content") and msg.get("role") != "system"  # Skip system and empty messages
                    ]
                    
                return messages
                
            return []
            
        except Exception as e:
            self.logger.error(f"Error formatting messages: {e}")
            raise LLMError(f"Failed to format messages: {e}")

    async def _make_validation_request(self) -> Any:
        """Make minimal test request to Anthropic."""
        return await self._client.messages.create(
            model=self.model_type,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )

    def _check_validation_response(self, response: Any) -> None:
        """Validate Anthropic response."""
        if not response or not response.content:
            raise ValueError("Invalid response from Anthropic")

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from last_response.

        Returns dict with prompt_tokens, completion_tokens, total_tokens.
        Returns None values if usage data not available.

        Note: Anthropic uses input_tokens/output_tokens, we map to standard names.
        """
        if not self.last_response:
            self.logger.warning("No last_response available for usage extraction")
            return {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}

        if not hasattr(self.last_response, 'usage') or not self.last_response.usage:
            self.logger.warning(f"No usage data in response for model {self.model_type}")
            return {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}

        usage = self.last_response.usage
        # Anthropic uses input_tokens/output_tokens instead of prompt/completion
        input_tokens = getattr(usage, 'input_tokens', None)
        output_tokens = getattr(usage, 'output_tokens', None)

        # Anthropic reports cache reads/writes SEPARATELY and EXCLUDES them from
        # input_tokens (total input = input_tokens + cache_read + cache_creation).
        # Fold them back so the meter sees full input, expose cache READS as
        # cached_tokens (billed at 0.1x) and cache CREATION as cache_creation_tokens
        # (G3: billed at 1.25x via ModelPricing.cache_write_price downstream).
        cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
        cache_creation = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        total_input = None if input_tokens is None else input_tokens + cache_read + cache_creation

        # Calculate total
        total_tokens = None
        if total_input is not None and output_tokens is not None:
            total_tokens = total_input + output_tokens

        usage_data = {
            'prompt_tokens': total_input,
            'completion_tokens': output_tokens,
            'total_tokens': total_tokens,
            'cached_tokens': cache_read,
            'cache_creation_tokens': cache_creation
        }

        self.logger.debug(f"Extracted usage: {usage_data}")
        return usage_data