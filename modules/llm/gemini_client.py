"""Google Gemini LLM API Client."""

from modules.llm.billing_guard import billed_google_options
import logging
import asyncio
from typing import List, Dict, Any, Optional, Union, Tuple
import json
import os
import warnings
from uuid import uuid4
# google.generativeai emits a noisy package-level FutureWarning ("all support has
# ended") at import. We're aware (migration tracked separately); silence it so it
# doesn't corrupt the CLI's first screen.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai
    import google.ai.generativelanguage as glm

from modules.llm.call_scope import call_timeout_param
from modules.llm.llm_client import LLMClient, raise_llm_error, translate_llm_error
from modules.llm.token_counter import count_messages_tokens
from modules.llm.gemini_parts import (
    function_call_args, json_schema_to_glm, part_is_function_call, read_candidate, tool_call_fields,
    tool_names_by_id, wire_system,
)
from core.exceptions import LLMConnectionError, LLMRateLimitError, ServiceError
from core.config import BotConfig

class GeminiClient(LLMClient):
    """Google Gemini LLM client.
    
    All model configuration comes from model_registry (single source of truth).
    Deprecated fallback dicts removed Dec 13, 2025.
    """

    def __init__(self, config: BotConfig, name: str = "gemini_client", container=None):
        """Initialize the client."""
        super().__init__(config=config, name=name, container=container)
        self._client = None
        
        # Model and API configuration
        llm_config = config.get_llm_config()
        gemini_config = llm_config.get('gemini', {})
        # Default: the operator pin / registry default (get_default_model)
        from modules.llm.llm_client_registry import get_default_model
        self.model_type = gemini_config.get('model') or get_default_model('gemini')
        
        # API key priority: 1) gemini config, 2) GEMINI_API_KEY env, 3) GOOGLE_API_KEY env
        self.api_key = (
            gemini_config.get('api_key') or
            os.environ.get('GEMINI_API_KEY') or
            os.environ.get('GOOGLE_API_KEY')
        )
        
        # Get defaults from model_registry (single source of truth)
        from modules.llm.model_registry import get_model_config
        model_config = get_model_config(self.model_type)
        
        # Get model limits from registry
        self.max_tokens = model_config.max_completion_tokens or 8192

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = 0.7  # Default temperature

        self.logger.debug(
            f"Gemini client initialized: model={self.model_type}, "
            f"supports_vision={self.supports_vision}"
        )

    def _is_gemini_3_model(self) -> bool:
        """Gemini 3 requires a thought signature on a replayed function call, and the pinned
        SDK's ``Part`` has no such field, so this client replays those turns as text."""
        if not self.model_type:
            return False
        model_lower = self.model_type.lower()
        # Match gemini-3-*, gemini3*, etc.
        return 'gemini-3' in model_lower or 'gemini3' in model_lower

    def _convert_content_to_parts(self, content: Any) -> List[Dict[str, Any]]:
        """Convert message content to Gemini parts format.
        
        Handles:
        - String content (text)
        - List of content parts (multimodal - text, images)
        - OpenAI image_url format -> Gemini inline_data format
        
        Args:
            content: Message content (string or list of parts)
            
        Returns:
            List of Gemini part dictionaries
        """
        parts = []
        
        # Handle string content
        if isinstance(content, str):
            if content.strip():
                parts.append({"text": content})
            return parts
        
        # Handle list content (multimodal)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    if item.strip():
                        parts.append({"text": item})
                elif isinstance(item, dict):
                    item_type = item.get("type", "")
                    
                    # Text part
                    if item_type == "text":
                        text = item.get("text", "")
                        if text.strip():
                            parts.append({"text": text})
                    
                    # Image part - OpenAI format (image_url)
                    elif item_type == "image_url":
                        image_part = self._convert_image_url_to_gemini(item)
                        if image_part:
                            parts.append(image_part)
                    
                    # Image part - Anthropic format (image with source)
                    elif item_type == "image":
                        image_part = self._convert_image_to_gemini(item)
                        if image_part:
                            parts.append(image_part)
                    
                    # Already Gemini format (inline_data)
                    elif "inline_data" in item:
                        parts.append(item)
                    
                    else:
                        self.logger.warning(f"Unknown content part type: {item_type}")
        
        return parts
    
    def _convert_image_url_to_gemini(self, image_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert OpenAI image_url format to Gemini inline_data format.
        
        OpenAI format:
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        
        Gemini format:
            {"inline_data": {"mime_type": "image/png", "data": "..."}}
        
        Args:
            image_item: Image in OpenAI format
            
        Returns:
            Image in Gemini format or None if conversion fails
        """
        try:
            image_url_data = image_item.get("image_url", {})
            url = image_url_data.get("url", "")
            
            if not url:
                self.logger.warning("Empty image URL in image_url format")
                return None
            
            # Handle base64 data URLs
            if url.startswith("data:"):
                try:
                    # Parse data URL: data:image/png;base64,<data>
                    header, base64_data = url.split(",", 1)
                    
                    # Extract MIME type from header
                    mime_type = "image/png"  # Default
                    if header.startswith("data:"):
                        header_content = header[5:]  # Remove "data:"
                        if ";" in header_content:
                            mime_type = header_content.split(";")[0]
                        elif header_content:
                            mime_type = header_content
                    
                    return {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64_data
                        }
                    }
                except ValueError as e:
                    self.logger.error(f"Failed to parse base64 data URL: {e}")
                    return None
            
            # Handle regular URLs - Gemini doesn't support URL images directly
            # Would need to download and convert to base64
            elif url.startswith("http://") or url.startswith("https://"):
                self.logger.warning(f"Gemini doesn't support URL images directly. URL: {url[:50]}...")
                return {"text": "[IMAGE: URL images not supported by Gemini]"}
            
            else:
                self.logger.warning(f"Unknown image URL format: {url[:50]}...")
                return None
                
        except Exception as e:
            self.logger.error(f"Error converting image_url to Gemini format: {e}")
            return None
    
    def _convert_image_to_gemini(self, image_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert Anthropic image format to Gemini inline_data format.
        
        Anthropic format:
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
        
        Gemini format:
            {"inline_data": {"mime_type": "image/png", "data": "..."}}
        
        Args:
            image_item: Image in Anthropic format
            
        Returns:
            Image in Gemini format or None if conversion fails
        """
        try:
            source = image_item.get("source", {})
            source_type = source.get("type", "")
            
            if source_type == "base64":
                return {
                    "inline_data": {
                        "mime_type": source.get("media_type", "image/png"),
                        "data": source.get("data", "")
                    }
                }
            elif source_type == "url":
                url = source.get("url", "")
                self.logger.warning(f"Gemini doesn't support URL images directly. URL: {url[:50]}...")
                return {"text": "[IMAGE: URL images not supported by Gemini]"}
            else:
                self.logger.warning(f"Unknown image source type: {source_type}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error converting image to Gemini format: {e}")
            return None

    def _call_timeout(self) -> float:
        """The step's time budget inside a step, else the configured request limit."""
        return call_timeout_param().get("timeout") or self._sdk_limits()["timeout"]

    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError("Gemini API key not provided. Set GEMINI_API_KEY or GOOGLE_API_KEY environment variable or configure in config.json under llm.gemini.api_key")
        
    async def _setup_client(self) -> None:
        """Set up the Gemini client."""
        try:
            # Configure the Gemini API with the API key
            genai.configure(api_key=self.api_key)
            self.logger.debug("Gemini client setup completed with API key: ****...****")
        except Exception as e:
            self.logger.error(f"Failed to set up Gemini client: {e}")
            raise ServiceError(f"Failed to set up Gemini client: {e}")
        
    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return
        try:
            self._validate_llm_config()
            await self._setup_client()

            if not self._skip_validate:
                await self._validate_connection()

            self._initialized = True
            self.logger.info(f"✅ Gemini client initialized with model {self.model_type}")

        except Exception as e:
            self.logger.error(f"Failed to initialize Gemini client: {e}")
            raise ServiceError(f"Failed to initialize Gemini client: {e}")

    async def _generate(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from Gemini API."""
        
        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()
                
            # Convert messages to Gemini format
            gemini_messages = []
            system_instruction_text = wire_system(system, messages)
            tool_names = tool_names_by_id(messages)

            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')

                if role == 'system':
                    continue

                # Handle tool role in non-tools path
                # When _generate is called without tools but history has tool messages,
                # convert them to text representations to avoid format errors
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', '')
                    tool_name = msg.get('name') or tool_names.get(tool_call_id, '')
                    # Convert tool response to text format for non-tool generation
                    tool_text = f"[Tool Result ({tool_name or tool_call_id})]: {content}"
                    # Add as user message (tool responses are from user's perspective)
                    if gemini_messages and gemini_messages[-1]["role"] == "user":
                        gemini_messages[-1]["parts"].append({"text": tool_text})
                    else:
                        gemini_messages.append({
                            "role": "user",
                            "parts": [{"text": tool_text}]
                        })
                    continue

                # Map roles to Gemini format
                if role == 'user':
                    gemini_role = 'user'
                elif role in ['assistant', 'ai']:
                    gemini_role = 'model'
                    # Handle assistant messages with tool_calls in non-tools path
                    # Convert tool calls to text representation
                    if 'tool_calls' in msg and msg['tool_calls']:
                        tool_calls_text = []
                        for tc in msg['tool_calls']:
                            if isinstance(tc, dict):
                                tc_name, tc_args = tool_call_fields(tc)
                                tool_calls_text.append(f"Called {tc_name} with {tc_args}")
                        if tool_calls_text:
                            combined = content + "\n[Tool Calls: " + "; ".join(tool_calls_text) + "]" if content else "[Tool Calls: " + "; ".join(tool_calls_text) + "]"
                            gemini_messages.append({
                                "role": gemini_role,
                                "parts": [{"text": combined}]
                            })
                            continue
                else:
                    self.logger.warning(f"Unknown role {role}, skipping message")
                    continue

                # Handle multimodal content (text, images)
                # Content might be a string or list of parts (OpenAI multimodal format)
                parts = self._convert_content_to_parts(content)
                if parts:
                    gemini_messages.append({
                        "role": gemini_role,
                        "parts": parts
                    })

            # System prompt is passed natively via system_instruction on the
            # GenerativeModel below (see system_instruction_text capture above) —
            # no need to inject a synthetic user/model turn pair anymore.

            # Use provided parameters or default to instance attributes
            temp = temperature if temperature is not None else self.temperature

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=self._estimate_input_tokens(messages, system_instruction_text),
            )

            # Log request details with proper values being used
            self.logger.info(f"Gemini API request: model={self.model_type}, temperature={temp}, max_tokens={max_tokens_value}")
            
            # Filter kwargs to only include supported parameters
            supported_params = {
                'safety_settings', 'top_p', 'top_k', 'stop_sequences', 'stream',
                'response_mime_type', 'candidate_count'
            }
            
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_params}
            
            # Get the Gemini model
            model = genai.GenerativeModel(
                model_name=self.model_type,
                generation_config={
                    "temperature": temp,
                    "max_output_tokens": max_tokens_value,
                    **filtered_kwargs
                },
                system_instruction=system_instruction_text if system_instruction_text else None
            )
            
            self.last_response = await asyncio.wait_for(
                model.generate_content_async(gemini_messages, **billed_google_options()),
                timeout=self._call_timeout())
            response_text, _ = read_candidate(self.last_response, self.model_type)


                
            return response_text
            
        except Exception as e:
            self.logger.error(f"Gemini API error: {e}")
            
            raise_llm_error(e, "Gemini", model=self.model_type)

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
            # Handle prompt formats (shared base normalization; system stays a
            # separate kwarg — a dict prompt's 'system' is hoisted into it)
            formatted_messages, system = self._normalize_prompt(
                prompt, messages, system, hoist_dict_system=True
            )

            # Gemini doesn't support metadata in the same way as other providers
            # Convert metadata to a more standard format if needed
            filtered_kwargs = kwargs.copy()
            
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
            raise_llm_error(e, "Gemini", model=self.model_type)

    async def _validate_connection(self) -> None:
        """Validate connection to Gemini."""
        try:
            # Simple test completion
            model = genai.GenerativeModel(
                model_name=self.model_type,
                generation_config={
                    "max_output_tokens": 10
                }
            )

            # Use native async method for validation
            response = await model.generate_content_async("Test connection")

            if not response:
                raise LLMConnectionError("No response from Gemini API")
            self.logger.debug("Gemini API connection validated successfully")
        except Exception as e:
            # Route through the unified classifier to detect rate-limit signals
            # (429, Resource exhausted, quota) — these are temporary; suppress and
            # continue initialisation so the client is available for real requests.
            translated = translate_llm_error(e, "Gemini validation")
            if isinstance(translated, LLMRateLimitError):
                self.logger.warning(
                    f"Gemini API temporarily rate-limited during validation (429). "
                    f"Client will still be initialized and will retry on actual requests: {e}"
                )
                return  # Continue with initialization

            # For other errors, fail initialization
            self.logger.error(f"Failed to validate Gemini connection: {e}")
            raise ServiceError(f"Failed to validate Gemini connection: {e}")

    async def _cleanup_client(self) -> None:
        """Clean up Gemini client."""
        # No specific cleanup needed for Gemini client
        self.logger.debug("Gemini client resources released")

    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._initialized = False
        self.logger.info("Gemini client cleaned up")

    async def _make_validation_request(self) -> Any:
        """Make minimal test request to Gemini."""
        model = genai.GenerativeModel(
            model_name=self.model_type,
            generation_config={"max_output_tokens": 1}
        )
        return await model.generate_content_async("test")

    def _check_validation_response(self, response: Any) -> None:
        """Validate Gemini response."""
        if not response:
            raise ValueError("Invalid response from Gemini")

    async def generate_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]]:
        """Generate response with tool-calling support for agents.

        This method bridges the adapter contract to enable native tool-calling
        for Gemini. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in provider format
            **kwargs: Additional generation parameters (system, temperature, etc.)

        Returns:
            Tuple of (content, tool_calls, usage_data). usage_data contains prompt_tokens,
            completion_tokens, total_tokens.

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

            self.logger.info(f"[generate_agent_response] Returning {len(result[1])} tool calls")
            return result

        except Exception as e:
            self.logger.error(f"generate_agent_response failed: {e}", exc_info=True)
            raise_llm_error(e, "Gemini", model=self.model_type)

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,  # ✅ ADD THIS
        temperature: Optional[float] = None,  # ✅ ADD THIS
        max_tokens: Optional[int] = None,  # ✅ ADD THIS
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]]:
        """Generate response with tools support.

        Args:
            messages: List of messages
            tools: List of tools
            system: System prompt (CRITICAL for brain state instructions)
            temperature: Temperature setting
            max_tokens: Max completion tokens
            metadata: Optional metadata

        Returns:
            Tuple of (text, tool_calls, usage_data)
        """
        try:
            if not tools:
                text = await self._generate(
                    messages=messages,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    metadata=metadata,
                    **kwargs
                )
                return text, [], self._extract_usage_data()

            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()

            # Convert messages to Gemini format
            gemini_messages = []

            system_content = wire_system(system, messages)
            tool_names = tool_names_by_id(messages)

            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')

                if role == 'system':
                    continue

                # Tool results: a FunctionResponse part, named from the prior assistant row.
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', '')
                    func_name = msg.get('name') or tool_names.get(tool_call_id)

                    # A result with no name to match its call, and any Gemini 3 result, goes as text.
                    if not func_name or self._is_gemini_3_model():
                        tool_result_text = f"[Tool Result ({func_name or tool_call_id})]: {content}"
                        if gemini_messages and gemini_messages[-1]["role"] == "user":
                            gemini_messages[-1]["parts"].append({"text": tool_result_text})
                        else:
                            gemini_messages.append({
                                "role": "user",
                                "parts": [{"text": tool_result_text}]
                            })
                    else:
                        # Non-Gemini-3: Use proper FunctionResponse parts
                        try:
                            function_response = glm.FunctionResponse(
                                name=func_name,
                                response={"result": content}
                            )
                            part = glm.Part(function_response=function_response)

                            if gemini_messages and gemini_messages[-1]["role"] == "user":
                                gemini_messages[-1]["parts"].append(part)
                            else:
                                gemini_messages.append({
                                    "role": "user",
                                    "parts": [part]
                                })
                        except Exception as e:
                            self.logger.warning(f"[HISTORY] Failed to create FunctionResponse part: {e}")
                            fallback_text = f"[Tool Response for {func_name}]: {content}"
                            if gemini_messages and gemini_messages[-1]["role"] == "user":
                                gemini_messages[-1]["parts"].append({"text": fallback_text})
                            else:
                                gemini_messages.append({
                                    "role": "user",
                                    "parts": [{"text": fallback_text}]
                                })
                    continue

                # Map roles to Gemini format
                if role == 'user':
                    gemini_role = 'user'
                elif role in ['assistant', 'ai']:
                    gemini_role = 'model'

                    # Tool calls: FunctionCall parts; Gemini 3 replays them as text, see
                    # _is_gemini_3_model.
                    if 'tool_calls' in msg and msg['tool_calls']:
                        if self._is_gemini_3_model():
                            tool_calls_text = []
                            for tc in msg['tool_calls']:
                                if isinstance(tc, dict):
                                    tc_name, tc_args = tool_call_fields(tc)
                                    if isinstance(tc_args, str):
                                        try:
                                            tc_args = json.loads(tc_args) if tc_args else {}
                                        except (json.JSONDecodeError, ValueError):
                                            pass
                                    tool_calls_text.append(f"[Called {tc_name} with args: {tc_args}]")

                            combined_text = content if content else ""
                            if tool_calls_text:
                                combined_text = (combined_text + "\n" if combined_text else "") + "\n".join(tool_calls_text)

                            if combined_text.strip():
                                gemini_messages.append({
                                    "role": gemini_role,
                                    "parts": [{"text": combined_text}]
                                })
                            continue
                        else:
                            # Non-Gemini-3: Use proper FunctionCall parts
                            parts = []

                            # Add text content if present
                            if content:
                                text_content = content if isinstance(content, str) else str(content)
                                if text_content.strip():
                                    parts.append(glm.Part(text=text_content))

                            # Add function call parts
                            for tc in msg['tool_calls']:
                                if isinstance(tc, dict):
                                    tc_name, tc_args = tool_call_fields(tc)
                                    if isinstance(tc_args, str):
                                        try:
                                            tc_args = json.loads(tc_args) if tc_args else {}
                                        except (json.JSONDecodeError, ValueError):
                                            tc_args = {"raw": tc_args}

                                    try:
                                        function_call = glm.FunctionCall(name=tc_name, args=tc_args)
                                        part = glm.Part(function_call=function_call)
                                        parts.append(part)
                                    except Exception as e:
                                        self.logger.warning(f"[HISTORY] Failed to create FunctionCall part: {e}")
                                        continue

                            if parts:
                                gemini_messages.append({
                                    "role": gemini_role,
                                    "parts": parts
                                })
                            continue
                else:
                    self.logger.warning(f"Unknown role {role}, treating as user")
                    gemini_role = 'user'

                # Convert content to Gemini parts format
                parts = self._convert_content_to_parts(content)

                # Skip if no valid parts
                if not parts:
                    continue

                # Merge consecutive messages with same role
                # Gemini requires alternating user/model messages
                if gemini_messages and gemini_messages[-1]["role"] == gemini_role:
                    # Append to existing message with same role
                    gemini_messages[-1]["parts"].extend(parts)
                else:
                    # Create new message
                    gemini_messages.append({
                        "role": gemini_role,
                        "parts": parts
                    })

            # ✅ Issue #5 fix: Use native system_instruction instead of injecting messages
            if system_content:
                self.logger.debug(f"Using native system_instruction ({len(system_content)} chars)")

            # Use explicit parameters
            temp = temperature if temperature is not None else self.temperature

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=self._estimate_input_tokens(messages, system_content),
                tools=tools,
            )

            self.logger.info(f"Gemini API request with tools: model={self.model_type}, tools={len(tools)}, max_tokens={max_tokens_value}, has_system={bool(system_content)}")

            # Convert tools to Gemini format
            gemini_tools = []

            for tool in tools:
                # ✅ Issue #6 fix: Validate before converting
                if not self._validate_tool_schema(tool):
                    self.logger.warning(f"Skipping invalid tool schema: {tool.get('name', 'unknown') if isinstance(tool, dict) else getattr(tool, 'name', 'unknown')}")
                    continue
                # Handle different tool formats (OpenAI, etc.)
                if isinstance(tool, dict):
                    # Already in Gemini format: {'function_declarations': [{...}]}
                    if 'function_declarations' in tool:
                        # Tool is already in Gemini format, use directly
                        gemini_tools.append(tool)
                    # OpenAI format: {'type': 'function', 'function': {...}}
                    elif 'type' in tool and tool['type'] == 'function':
                        gemini_tools.append({
                            "function_declarations": [
                                {
                                    "name": tool['function']['name'],
                                    "description": tool['function'].get('description', ''),
                                    "parameters": tool['function'].get('parameters', {})
                                }
                            ]
                        })
                    # Direct format: {'name': ..., 'description': ..., 'parameters': ...}
                    elif 'name' in tool:
                        gemini_tools.append({
                            "function_declarations": [
                                {
                                    "name": tool['name'],
                                    "description": tool.get('description', ''),
                                    "parameters": tool.get('parameters', tool.get('args_schema', {}))
                                }
                            ]
                        })
                    else:
                        self.logger.warning(f"Unknown tool format: {tool}")
                else:
                    # Tool object
                    if hasattr(tool, 'name'):
                        parameters = {}
                        if hasattr(tool, 'args_schema'):
                            # Convert Pydantic model to JSON schema
                            if tool.args_schema:
                                parameters = tool.args_schema.model_json_schema()
                        elif hasattr(tool, 'args'):
                            parameters = tool.args

                        gemini_tools.append({
                            "function_declarations": [
                                {
                                    "name": tool.name,
                                    "description": getattr(tool, 'description', ''),
                                    "parameters": parameters
                                }
                            ]
                        })
                    else:
                        self.logger.warning(f"Unknown tool object type: {type(tool)}")

            # Convert dict-based tools to proper glm.Tool objects
            tool_objects = []
            if gemini_tools:
                for tool_dict in gemini_tools:
                    if isinstance(tool_dict, dict) and 'function_declarations' in tool_dict:
                        function_declarations = []
                        for func in tool_dict['function_declarations']:
                            # Create FunctionDeclaration
                            try:
                                function_declarations.append(
                                    glm.FunctionDeclaration(
                                        name=func['name'],
                                        description=func.get('description', ''),
                                        parameters=json_schema_to_glm(glm, func.get('parameters', {'type': 'object'}))
                                    )
                                )
                            except Exception as e:
                                self.logger.error(f"Failed to convert function '{func.get('name', 'unknown')}' to schema: {e}")
                                self.logger.error(f"Function parameters: {func.get('parameters', {})}")
                                # Skip this function
                                continue

                        # Create Tool object
                        tool_objects.append(glm.Tool(function_declarations=function_declarations))
                        self.logger.debug(f"Created glm.Tool with {len(function_declarations)} function declarations")

            # Create generation config (extracted for reuse in retry logic)
            generation_config = {
                "temperature": temp,
                "max_output_tokens": max_tokens_value,
            }
            if kwargs.get('stop_sequences'):
                generation_config["stop_sequences"] = kwargs['stop_sequences']

            model = genai.GenerativeModel(
                model_name=self.model_type,
                generation_config=generation_config,
                tools=tool_objects if tool_objects else None,
                system_instruction=system_content if system_content else None
            )

            # NOT prefix-stamped — Gemini hands the SDK protobuf `Tool`
            # objects, not a dict request, and a digest of their repr is not
            # stable across SDK versions. An unstable stamp is worse than none.

            # Log API request details
            func_count = sum(len(t.function_declarations) for t in tool_objects if hasattr(t, 'function_declarations')) if tool_objects else 0
            self.logger.info(
                f"Gemini API call: model={self.model_type}, messages={len(gemini_messages)}, "
                f"functions={func_count}, system={len(system_content) if system_content else 0}chars"
            )

            # Debug: Log function names
            if tool_objects and self.logger.isEnabledFor(logging.DEBUG):
                func_names = [f.name for t in tool_objects if hasattr(t, 'function_declarations') for f in t.function_declarations]
                self.logger.debug(f"Available functions: {func_names}")

            response = await asyncio.wait_for(
                model.generate_content_async(gemini_messages, **billed_google_options()),
                timeout=self._call_timeout(),
            )
            self.last_response = response

            response_text, function_parts = read_candidate(response, self.model_type)
            tool_calls = []
            for part in function_parts:
                function_call = part.function_call
                name = getattr(function_call, 'name', None)
                if not name or not name.strip():
                    self.logger.error(f"Gemini function_call missing name: {repr(function_call)[:500]}")
                    continue
                tool_calls.append({
                    "id": f"gemini_call_{uuid4()}",
                    "type": "function",
                    "function": {"name": name, "arguments": function_call_args(function_call)},
                })

            usage_data = self._extract_usage_data()

            return response_text, tool_calls, usage_data

        except Exception as e:
            self.logger.error(f"Gemini API tool-based generation error: {e}")
            raise_llm_error(e, "Gemini", model=self.model_type)

    def _estimate_input_tokens(self, messages: List[Dict[str, Any]], system: Optional[str]) -> int:
        """The rows and the system prompt, with the system prompt counted once."""
        rows = [m for m in messages if m.get('role') != 'system']
        if system:
            rows = [{"role": "system", "content": system}, *rows]
        return count_messages_tokens(rows, self.model_type)

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from last_response.

        completion_tokens holds the thought tokens, which Gemini bills as output. With no
        usage_metadata the output is estimated from the text and ``usage_estimated`` says so;
        the input of such a call is unknown and reads 0. A count is never None.
        """
        # Initialize defaults
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None
        cached_tokens = 0  # Gemini implicit cache hits

        if not self.last_response:
            self.logger.warning("No last_response available for usage extraction")
        elif hasattr(self.last_response, 'usage_metadata') and self.last_response.usage_metadata:
            # Extract from Gemini usage_metadata
            usage = self.last_response.usage_metadata
            prompt_tokens = getattr(usage, 'prompt_token_count', None)
            completion_tokens = getattr(usage, 'candidates_token_count', None)
            thoughts = getattr(usage, 'thoughts_token_count', None)
            if isinstance(thoughts, int) and thoughts and completion_tokens is not None:
                completion_tokens += thoughts
            total_tokens = getattr(usage, 'total_token_count', None)
            cached_tokens = getattr(usage, 'cached_content_token_count', 0) or 0

            # Calculate total if not provided
            if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens

        estimated = False
        if completion_tokens is None and self.last_response:
            text = self._response_text_for_estimate()
            if text:
                completion_tokens = len(text) // 4
                estimated = True
                self.logger.info(f"Estimated Gemini output tokens: {completion_tokens} (usage_metadata unavailable)")

        # Final validation: Never return None - use 0 as last resort
        usage_data = {
            'prompt_tokens': prompt_tokens if prompt_tokens is not None else 0,
            'completion_tokens': completion_tokens if completion_tokens is not None else 0,
            'total_tokens': total_tokens if total_tokens is not None else (prompt_tokens or 0) + (completion_tokens or 0),
            'cached_tokens': cached_tokens,
        }
        if estimated:
            usage_data['usage_estimated'] = True

        if usage_data['total_tokens'] == 0:
            self.logger.warning("No usage data available and estimation failed - using 0 tokens")

        self.logger.debug(f"Extracted usage: {usage_data}")
        return usage_data

    def _response_text_for_estimate(self) -> str:
        try:
            return read_candidate(self.last_response, self.model_type)[0]
        except Exception:
            return ""
