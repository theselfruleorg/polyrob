"""DeepSeek LLM API Client.

Refactored Dec 2025: All model configuration comes from model_registry.
No deprecated fallback constants - registry is the single source of truth.
"""

import logging
import asyncio
import time
from typing import List, Dict, Any, Optional, Union, Tuple
import requests

from modules.llm.llm_client import LLMClient
from modules.llm.token_counter import count_messages_tokens
from modules.llm.model_registry import get_model_config
from core.exceptions import LLMError, ServiceError
from core.config import BotConfig


class DeepSeekClient(LLMClient):
    """DeepSeek LLM client.

    All model capabilities and limits come from model_registry.
    """

    # DeepSeek-specific: max chain-of-thought tokens for reasoner models
    # This is not in model_registry as it's a DeepSeek-specific API parameter
    DEFAULT_MAX_COT_TOKENS = 32000

    def _supports_tools(self) -> bool:
        """Check if current model supports tool calling via model_registry."""
        config = get_model_config(self.model_type)
        if config and config.capabilities:
            return config.capabilities.supports_function_calling
        return False

    def _is_reasoner_model(self) -> bool:
        """Check if current model is a reasoning/thinking model."""
        config = get_model_config(self.model_type)
        if config and config.capabilities:
            return config.capabilities.supports_thinking
        return False

    def _get_headers(self) -> Dict[str, str]:
        """Get standard API request headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def _build_request_body(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Build the API request body with common logic.

        Args:
            messages: Messages to send
            temperature: Temperature setting
            max_tokens: Max tokens (will be adjusted)
            tools: Optional tool definitions
            **kwargs: Additional parameters

        Returns:
            Request body dict ready for API call
        """
        temp = temperature if temperature is not None else self.temperature
        max_tokens_value = self._adjust_max_tokens(messages, max_tokens)

        # Base request body
        request_body = {
            'model': self.model_type,
            'messages': messages,
            'temperature': temp,
            'max_tokens': max_tokens_value
        }

        # Add tools if provided
        if tools:
            request_body['tools'] = tools
            request_body['tool_choice'] = 'auto'

        # Add supported parameters from kwargs
        supported_params = {
            'top_p', 'top_k', 'stream', 'stop', 'response_format'
        }
        for key, value in kwargs.items():
            if key in supported_params:
                request_body[key] = value

        # Special handling for reasoner/thinking models. UP-07: source the COT budget
        # from the registry (single source of truth) when the thinking-config gate is on;
        # an explicit kwargs override always wins, and the const remains the fallback.
        if self._is_reasoner_model():
            default_cot = self.DEFAULT_MAX_COT_TOKENS
            try:
                from modules.llm.model_registry import thinking_config_enabled, get_thinking_config
                if thinking_config_enabled():
                    budget = get_thinking_config(self.model_type).get("budget_tokens")
                    if budget:
                        default_cot = budget
            except Exception:
                pass
            max_cot_tokens = kwargs.get("max_cot_tokens", default_cot)
            if max_cot_tokens > 0:
                request_body["max_cot_tokens"] = max_cot_tokens

        return request_body

    def __init__(self, config: BotConfig, name: str = "deepseek_client"):
        """Initialize the client."""
        super().__init__(config=config, name=name)
        self._client = None

        # Model and API configuration
        deepseek_config = config.get_llm_config()['deepseek']
        self.model_type = deepseek_config.get('model', 'deepseek-chat')
        self.api_key = deepseek_config.get('api_key')
        self.api_base = deepseek_config.get('api_url', 'https://api.deepseek.com/v1')
        self.last_response = None

        # Get model config from registry (single source of truth)
        model_config = get_model_config(self.model_type)
        if not model_config:
            raise ServiceError(f"Model '{self.model_type}' not found in model_registry")

        # Set from registry (model must exist — checked above)
        self.max_tokens = model_config.max_completion_tokens
        # Resolve vision support via base helper (registry value is authoritative here
        # since DeepSeek always requires a registered model)
        self.supports_vision = self._resolve_supports_vision()
        self.temperature = 0.7  # DeepSeek default

        self.logger.debug(
            f"DeepSeek client initialized: model={self.model_type}, "
            f"max_tokens={self.max_tokens}, supports_vision={self.supports_vision}"
        )
        
    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError("DeepSeek API key not provided")
        
    async def _setup_client(self) -> None:
        """Set up the DeepSeek client.
        
        For DeepSeek API, we don't need a persistent client object,
        but we implement this method to satisfy the abstract requirement.
        """
        # No special setup needed for API-based client
        self.logger.debug("DeepSeek client setup - no persistent client needed")
        pass
        
    async def _validate_connection(self) -> None:
        """Validate DeepSeek API connection.

        Delegates to validate() to avoid code duplication.
        """
        try:
            await self.validate()
            self.logger.debug("DeepSeek API connection validated successfully")
        except (ValueError, LLMError) as e:
            # validate() now routes failures through _translate_error -> LLMError
            # (never bare ValueError), so catch both and surface as ServiceError.
            raise ServiceError(f"DeepSeek API connection validation failed: {str(e)}")
        
    async def _cleanup_client(self) -> None:
        """Clean up DeepSeek client resources.
        
        For DeepSeek API, there's no persistent client to clean up,
        but we implement this method to satisfy the abstract requirement.
        """
        # No special cleanup needed for API-based client
        self.logger.debug("DeepSeek client cleanup - no persistent resources to release")
        pass
            
    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return
            
        try:
            # Log initialization attempt
            self.logger.info(f"Initializing DeepSeek client with model {self.model_type}")
            
            # Validate API key
            self._validate_llm_config()
                
            # Test connection (skipped for fast startup; validates lazily)
            if not self._skip_validate:
                await self._validate_connection()
            
            # Mark as initialized
            self._initialized = True
            
            # Log successful initialization
            self.logger.info(f"✨ DeepSeek client initialized successfully with model {self.model_type}")
            
        except Exception as e:
            # Single error log with clear cause
            error_msg = f"Failed to initialize DeepSeek client: {str(e)}"
            self.logger.error(error_msg)
            raise ServiceError(error_msg)
            
    async def _generate(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from the DeepSeek API."""
        start_time = time.time()
        success = False
        error_message = None

        try:
            if not self._initialized:
                await self._initialize()

            # Build request body using helper (handles token adjustment, reasoner, etc.)
            request_body = self._build_request_body(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            self.logger.debug(f"DeepSeek API request: model={self.model_type}, max_tokens={request_body.get('max_tokens')}")

            # Make the API request
            response = await asyncio.to_thread(
                requests.post,
                f"{self.api_base}/chat/completions",
                headers=self._get_headers(),
                json=request_body,
                timeout=self.DEFAULT_REQUEST_TIMEOUT
            )
            
            # Check for errors
            if response.status_code != 200:
                error_message = f"DeepSeek API error ({response.status_code}): {response.text}"
                self.logger.error(error_message)
                raise ServiceError(error_message)
                
            # Parse the response
            json_response = response.json()
            self.last_response = json_response
            
            # Mark as successful
            success = True
            
            # Extract the content
            response_text = ""
            if "choices" in json_response and json_response["choices"]:
                choice = json_response["choices"][0]
                if "message" in choice and "content" in choice["message"]:
                    response_text = choice["message"]["content"] or ""
            
            # Capture telemetry with actual timing and usage data
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))
                
            return response_text
            
        except requests.RequestException as e:
            success = False
            error_message = str(e)
            self.logger.error(f"DeepSeek API request error: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"DeepSeek API request error: {e}")
        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Error generating response from DeepSeek: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"Error generating response from DeepSeek: {e}")
            
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
            
            # Add system message if provided
            if system:
                has_system = any(msg.get('role') == 'system' for msg in formatted_messages)
                if not has_system:
                    formatted_messages.insert(0, {"role": "system", "content": system})
            
            # Filter out metadata that DeepSeek API doesn't support
            filtered_kwargs = kwargs.copy()
            
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
            
    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Surface DeepSeek's automatic context-cache hit count (UP-08).

        DeepSeek caching is on by default server-side; the API returns
        ``usage.prompt_cache_hit_tokens``. Mapping it to ``cached_tokens`` lets
        ``calculate_cost`` bill the cached portion at ``cached_input_price`` (the
        downstream plumbing already reads ``cached_tokens``). No request change.
        """
        base = super()._extract_usage_data()
        last_response = getattr(self, 'last_response', None)
        if isinstance(last_response, dict):
            usage = last_response.get('usage') or {}
            base['cached_tokens'] = usage.get('prompt_cache_hit_tokens', 0) or 0
        return base

    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._initialized = False
        self.logger.info("DeepSeek client cleaned up")

    async def _make_validation_request(self) -> Any:
        """Make minimal test request to DeepSeek."""
        data = {
            "model": self.model_type,
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 1
        }
        return await asyncio.to_thread(
            requests.post,
            f"{self.api_base}/chat/completions",
            headers=self._get_headers(),
            json=data,
            timeout=10
        )

    def _check_validation_response(self, response: Any) -> None:
        """Validate DeepSeek response."""
        if response.status_code != 200:
            raise ValueError(f"Invalid response from DeepSeek: {response.status_code} - {response.text}")

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Generate response with tool calling support (for models that support it).

        Args:
            messages: List of messages
            tools: List of tools in OpenAI format
            temperature: Temperature for generation
            max_tokens: Maximum tokens for generation
            **kwargs: Additional parameters

        Returns:
            Tuple of (text_response, tool_calls, usage_data)

        Note:
            Tool support check is done by caller (generate_agent_response).
            This method assumes the caller has already validated tool support.
        """
        start_time = time.time()
        success = False
        error_message = None

        try:
            if not self._initialized:
                await self._initialize()

            # Build request body using helper (handles token adjustment, reasoner, etc.)
            request_body = self._build_request_body(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                **kwargs
            )

            self.logger.info(f"[_generate_with_tools] Starting API request: model={self.model_type}, tools={len(tools)}, messages={len(messages)}")

            # Make the API request (longer timeout for tool calling)
            api_start = time.time()
            response = await asyncio.to_thread(
                requests.post,
                f"{self.api_base}/chat/completions",
                headers=self._get_headers(),
                json=request_body,
                timeout=180  # Increased timeout for large tool lists
            )
            api_duration = time.time() - api_start
            self.logger.info(f"[_generate_with_tools] API request completed in {api_duration:.1f}s, status={response.status_code}")

            # Check for errors
            if response.status_code != 200:
                error_message = f"DeepSeek API error ({response.status_code}): {response.text}"
                self.logger.error(error_message)
                raise ServiceError(error_message)

            # Parse the response
            json_response = response.json()
            self.last_response = json_response

            # Mark as successful
            success = True

            # Extract content and tool calls
            text_response = ""
            tool_calls = []

            if "choices" in json_response and json_response["choices"]:
                choice = json_response["choices"][0]
                message = choice.get("message", {})

                # Extract text content
                if "content" in message and message["content"]:
                    text_response = message["content"]

                # Extract tool calls
                if "tool_calls" in message and message["tool_calls"]:
                    tool_calls = message["tool_calls"]
                    self.logger.debug(f"Extracted {len(tool_calls)} tool calls from response")

            # Capture telemetry
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))

            # Extract usage data
            usage_data = self._extract_usage_data()

            return (text_response, tool_calls, usage_data)

        except requests.RequestException as e:
            success = False
            error_message = str(e)
            self.logger.error(f"DeepSeek API request error: {e}")
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            raise ServiceError(f"DeepSeek API request error: {e}")
        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Error generating response with tools from DeepSeek: {e}")
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            raise ServiceError(f"Error generating response with tools from DeepSeek: {e}")

    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Union[Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]], str]:
        """Generate response with tool-calling support for agents.

        This method bridges the adapter contract to enable native tool-calling
        for DeepSeek V3. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in OpenAI-compatible format
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
            NotImplementedError: If model doesn't support tool calling
        """
        try:
            self.logger.info(f"[generate_agent_response] Called with {len(messages)} messages and {len(tools) if tools else 0} tools")

            # Extract system message if present in kwargs
            system = kwargs.pop('system', None)
            temperature = kwargs.pop('temperature', None)
            max_tokens = kwargs.pop('max_tokens', None)

            # Add system message to messages if provided
            formatted_messages = []
            if system:
                formatted_messages.append({"role": "system", "content": system})
            formatted_messages.extend(messages)

            # Check if this model supports tool calling (uses model_registry as source of truth)
            if not self._supports_tools():
                self.logger.warning(
                    f"DeepSeek model {self.model_type} does not support native tool calling. "
                    "Raising NotImplementedError to trigger fallback."
                )
                raise NotImplementedError(
                    f"DeepSeek model {self.model_type} does not support native tool/function calling"
                )

            # Call the tool-enabled generation method
            content, tool_calls, usage_data = await self._generate_with_tools(
                messages=formatted_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            self.logger.info(f"[generate_agent_response] Returning {len(tool_calls)} tool calls + usage")

            # Always return the canonical 3-tuple: (content, tool_calls, usage)
            # tool_calls may be [] when the model replied without calling a tool.
            return (content, tool_calls, usage_data)

        except NotImplementedError:
            # Re-raise NotImplementedError to trigger fallback
            raise
        except Exception as e:
            self.logger.error(f"Error in generate_agent_response: {e}", exc_info=True)
            raise LLMError(f"Failed to generate agent response: {e}")