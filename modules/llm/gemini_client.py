"""Google Gemini LLM API Client."""

import logging
import asyncio
import time
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

from modules.llm.llm_client import LLMClient, translate_llm_error
from modules.llm.token_counter import count_messages_tokens
from core.exceptions import LLMError, LLMConnectionError, LLMRateLimitError, ServiceError
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
        
        # Default to gemini-2.5-flash (latest stable as of Nov 2025)
        self.model_type = gemini_config.get('model', 'gemini-2.5-flash')
        
        # API key priority: 1) gemini config, 2) GEMINI_API_KEY env, 3) GOOGLE_API_KEY env
        self.api_key = (
            gemini_config.get('api_key') or
            os.environ.get('GEMINI_API_KEY') or
            os.environ.get('GOOGLE_API_KEY')
        )
        
        # If we found a key, also set environment variable for libraries that use it directly
        if self.api_key:
            os.environ['GEMINI_API_KEY'] = self.api_key
            os.environ['GOOGLE_API_KEY'] = self.api_key
            
        self.last_response = None  # Store the last response for token usage reporting

        # UP-08: explicit cachedContents (opt-in, GEMINI_PROMPT_CACHE). A billed, TTL'd
        # server object reused across steps; busted on tool-set change; deleted on cleanup.
        self._cached_content = None
        self._cached_tool_sig = None

        # Get defaults from model_registry (single source of truth)
        from modules.llm.model_registry import get_model_config
        model_config = get_model_config(self.model_type)
        
        # Get model limits from registry
        if model_config:
            self.max_tokens = model_config.max_completion_tokens or 8192
        else:
            self.max_tokens = 8192  # Conservative fallback

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = 0.7  # Default temperature

        self.logger.debug(
            f"Gemini client initialized: model={self.model_type}, "
            f"supports_vision={self.supports_vision}"
        )

    def _is_gemini_3_model(self) -> bool:
        """Check if current model is Gemini 3 (requires thought signatures for function calling).

        Gemini 3 models enforce strict thought signature validation during function calling.
        When thought signatures are missing, the API may return malformed FunctionCall objects
        with empty name fields, causing tool calls to fail.

        Returns:
            True if model is Gemini 3 series, False otherwise
        """
        if not self.model_type:
            return False
        model_lower = self.model_type.lower()
        # Match gemini-3-*, gemini3*, etc.
        return 'gemini-3' in model_lower or 'gemini3' in model_lower

    # ---- UP-08 explicit cachedContents -------------------------------------
    @staticmethod
    def _tool_signature(tool_objects) -> str:
        """Stable signature of the tool schema so we can bust a stale cache."""
        names = []
        for t in (tool_objects or []):
            for f in getattr(t, 'function_declarations', []) or []:
                names.append(getattr(f, 'name', ''))
        return ",".join(sorted(names))

    @staticmethod
    def _cache_signature(system_instruction, tool_objects) -> str:
        """L1: reuse key for an explicit cachedContents object. The cached object bakes in
        BOTH the system_instruction AND the tools, so the reuse key MUST include both —
        keying on tool names alone let a session reuse another session's cached SYSTEM
        prompt (persona/SOUL/SELF/project-context) whenever the tool set matched."""
        import hashlib
        sys_hash = hashlib.sha256((system_instruction or "").encode("utf-8")).hexdigest()[:16]
        return f"{sys_hash}:{GeminiClient._tool_signature(tool_objects)}"

    def _delete_cached_content(self) -> None:
        """Delete the current cached object (fail-open) and clear local state."""
        cache = self._cached_content
        self._cached_content = None
        self._cached_tool_sig = None
        if cache is not None:
            try:
                cache.delete()
            except Exception as e:
                self.logger.debug(f"Gemini cache delete failed (ignored): {e}")

    def _maybe_build_cached_content(self, system_instruction, tool_objects):
        """Return a reusable CachedContent for the stable system+tools prefix, or None.

        UP-08, default OFF (GEMINI_PROMPT_CACHE). Scoped to the non-Gemini-3 tools path
        (Gemini-3 ChatSession + thought-signatures interact badly with cached content).
        Reused across steps; busted when the tool-set signature changes; fail-open to the
        normal uncached path on any SDK error. Requires an estimated prefix >= the API
        floor (~2048 tokens) to be worth a billed cache object.
        """
        from modules.llm.cache_hints import (
            gemini_explicit_cache_enabled, GEMINI_EXPLICIT_CACHE_MIN_TOKENS)
        if not gemini_explicit_cache_enabled():
            return None
        if self._is_gemini_3_model():
            return None
        # Rough prefix-size estimate (~4 chars/token): system text + tool schema names.
        est_chars = len(system_instruction or "") + len(self._tool_signature(tool_objects))
        if est_chars // 4 < GEMINI_EXPLICIT_CACHE_MIN_TOKENS:
            return None
        # L1: reuse key includes the system prompt, not just the tool set — a cached
        # object bakes in both, so busting only on tool change leaked the system prompt.
        sig = self._cache_signature(system_instruction, tool_objects)
        # Reuse within session unless the system prompt OR tool set changed.
        if self._cached_content is not None and self._cached_tool_sig == sig:
            return self._cached_content
        if self._cached_content is not None:
            self._delete_cached_content()  # stale prefix -> bust before recreate
        try:
            import google.generativeai as genai
            import datetime
            ttl_min = int(os.getenv("GEMINI_CACHE_TTL_MIN", "10"))
            cache = genai.caching.CachedContent.create(
                model=self.model_type,
                system_instruction=system_instruction or None,
                tools=tool_objects or None,
                ttl=datetime.timedelta(minutes=ttl_min),
            )
            self._cached_content = cache
            self._cached_tool_sig = sig
            self.logger.info(
                f"event=gemini_explicit_cache_created model={self.model_type} "
                f"tools={sig.count(',') + 1 if sig else 0} ttl_min={ttl_min}"
            )
            return cache
        except Exception as e:
            self.logger.warning(f"Gemini explicit cache create failed, continuing uncached: {e}")
            self._cached_content = None
            self._cached_tool_sig = None
            return None

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
        start_time = time.time()
        success = False
        error_message = None
        
        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()
                
            # Convert messages to Gemini format
            gemini_messages = []
            has_system = False
            # H1 FIX: capture the system prompt (from the `system` kwarg OR embedded as a
            # role='system' message) and pass it natively via system_instruction below.
            # Previously a system message in `messages` was skipped and never re-added
            # (the injection block only fired for the `system` kwarg), so every no-tools
            # Gemini call silently lost its system prompt.
            system_instruction_text = system if isinstance(system, str) else None

            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')

                if role == 'system':
                    has_system = True
                    # Prefer the content embedded in the message list — the agent builds
                    # the real system prompt there, not via the `system` kwarg.
                    if content and isinstance(content, str):
                        system_instruction_text = content
                    continue  # passed natively via system_instruction below

                # FIX (Dec 2, 2025): Handle tool role in non-tools path
                # When _generate is called without tools but history has tool messages,
                # convert them to text representations to avoid format errors
                if role == 'tool':
                    tool_name = msg.get('name', '')
                    tool_call_id = msg.get('tool_call_id', '')
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
                    # FIX (Dec 2, 2025): Handle assistant messages with tool_calls in non-tools path
                    # Convert tool calls to text representation
                    if 'tool_calls' in msg and msg['tool_calls']:
                        tool_calls_text = []
                        for tc in msg['tool_calls']:
                            if isinstance(tc, dict):
                                tc_name = tc.get('name') or (tc.get('function', {}).get('name') if 'function' in tc else 'unknown')
                                tc_args = tc.get('args') or tc.get('arguments') or (tc.get('function', {}).get('arguments') if 'function' in tc else {})
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

                # FIX (Dec 3, 2025): Properly handle multimodal content (text, images)
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

            # Estimate input tokens (messages + system, Gemini sends them separately)
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
            self.logger.info(f"Gemini API request: model={self.model_type}, temperature={temp}, max_tokens={max_tokens_value}, est_input_tokens={estimated_input_tokens}")
            
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
                system_instruction=system_instruction_text if system_instruction_text else None  # H1 FIX
            )
            
            # Make API request using native async method
            # FIX (Dec 2, 2025): Use generate_content_async instead of to_thread wrapper
            response = await model.generate_content_async(gemini_messages)
            
            self.last_response = response
            
            # Mark as successful
            success = True
            
            # Process response with robust error handling
            # FIX (Dec 2, 2025): Handle ProtoType DESCRIPTOR errors from protobuf version mismatches
            response_text = ""
            try:
                if hasattr(response, 'text'):
                    response_text = response.text
                elif hasattr(response, 'parts'):
                    text_parts = []
                    for part in response.parts:
                        if hasattr(part, 'text') and part.text:
                            text_parts.append(part.text)
                    response_text = " ".join(text_parts)
            except AttributeError as proto_err:
                # Handle protobuf version mismatch errors
                if 'DESCRIPTOR' in str(proto_err) or 'ProtoType' in str(proto_err):
                    self.logger.warning(f"Protobuf version mismatch, trying fallback: {proto_err}")
                    # Try to access raw response data as fallback
                    try:
                        if hasattr(response, '_result') and hasattr(response._result, 'candidates'):
                            for candidate in response._result.candidates:
                                if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                                    for part in candidate.content.parts:
                                        if hasattr(part, 'text'):
                                            response_text += part.text
                    except Exception as fallback_err:
                        self.logger.error(f"Fallback response extraction also failed: {fallback_err}")
                        raise ServiceError(f"Failed to extract response due to protobuf version mismatch: {proto_err}")
                else:
                    raise
            
            # Capture telemetry with actual timing and usage data
            self._extract_usage_and_capture_telemetry(start_time, success, None, kwargs.get('metadata'))
                
            return response_text
            
        except Exception as e:
            success = False
            error_message = str(e)
            self.logger.error(f"Gemini API error: {e}")
            
            # Capture telemetry for failed requests too
            self._extract_usage_and_capture_telemetry(start_time, success, error_message, kwargs.get('metadata'))
            
            raise ServiceError(f"Gemini API error: {e}")

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
            raise ServiceError(f"Failed to generate response: {e}")

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
        self._delete_cached_content()  # UP-08: never orphan a billed cachedContents object
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
    ) -> Union[Tuple[str, List[Dict[str, Any]], Dict[str, Optional[int]]], str]:
        """Generate response with tool-calling support for agents.

        This method bridges the adapter contract to enable native tool-calling
        for Gemini. It delegates to _generate_with_tools which handles the
        wire-format conversion and API interaction.

        Args:
            messages: List of message dictionaries
            tools: List of tool schemas in provider format
            **kwargs: Additional generation parameters (system, temperature, etc.)

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

            # Log the result type for debugging
            if isinstance(result, tuple):
                if len(result) == 3:
                    _, tool_calls, _ = result
                    self.logger.info(f"[generate_agent_response] Returning 3-tuple with {len(tool_calls)} tool calls")
                elif len(result) == 2:
                    _, tool_calls = result
                    self.logger.warning(f"[generate_agent_response] Got 2-tuple (old format) with {len(tool_calls)} tool calls")
            else:
                self.logger.info(f"[generate_agent_response] Returning content only")

            return result

        except LLMError:
            # Re-raise LLM errors as-is
            raise
        except Exception as e:
            # Wrap other exceptions using core.exceptions pattern
            self.logger.error(f"generate_agent_response failed: {str(e)}", exc_info=True)
            raise LLMError(f"Failed to generate agent response: {str(e)}")

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
        """Generate response with tools support.

        Args:
            messages: List of messages
            tools: List of tools
            system: System prompt (CRITICAL for brain state instructions)
            temperature: Temperature setting
            max_tokens: Max completion tokens
            metadata: Optional metadata

        Returns:
            Response text or tuple of (text, tool_calls)
        """
        try:
            if not tools:
                # If no tools, use standard generation.
                # H1 FIX: forward system/temperature/max_tokens so the system prompt
                # isn't lost on the no-tools fallthrough.
                return await self._generate(
                    messages=messages,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs
                )

            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()

            # Convert messages to Gemini format
            gemini_messages = []

            # ✅ FIX: Handle system message separately (skip from messages, prepend to content)
            system_content = system  # Use explicit system parameter

            for msg in messages:
                role = msg.get('role', '')
                content = msg.get('content', '')

                # Skip system messages - handle via system parameter
                if role == 'system':
                    if not system_content:
                        system_content = content if isinstance(content, str) else str(content)
                    continue

                # FIX (Dec 3, 2025): Handle tool response messages
                # For Gemini 3: FunctionResponse parts require thought_signature, which we don't
                # have when replaying history. Convert to text representation instead.
                # For non-Gemini-3: Use proper glm.Part with FunctionResponse
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', '')
                    tool_name = msg.get('name', '')
                    func_name = tool_name if tool_name else f"tool_{tool_call_id}"

                    # For Gemini 3, always use text to avoid thought_signature requirement
                    if self._is_gemini_3_model():
                        # Convert tool response to text representation
                        tool_result_text = f"[Tool Result ({func_name})]: {content}"
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

                    # FIX (Dec 3, 2025): Handle assistant messages with tool_calls
                    # For Gemini 3: FunctionCall parts require thought_signature, which we don't
                    # have when replaying history. Convert to text representation instead.
                    # For non-Gemini-3: Use proper glm.Part with FunctionCall
                    if 'tool_calls' in msg and msg['tool_calls']:
                        # Check if this is Gemini 3 - if so, convert to text to avoid thought_signature requirement
                        if self._is_gemini_3_model():
                            # Convert tool calls to text representation for Gemini 3
                            tool_calls_text = []
                            for tc in msg['tool_calls']:
                                if isinstance(tc, dict):
                                    tc_name = tc.get('name') or (tc.get('function', {}).get('name') if 'function' in tc else 'unknown')
                                    tc_args = tc.get('args') or tc.get('arguments') or (tc.get('function', {}).get('arguments') if 'function' in tc else {})
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
                                    tc_name = tc.get('name') or (tc.get('function', {}).get('name') if 'function' in tc else None)
                                    tc_args = tc.get('args') or tc.get('arguments') or (tc.get('function', {}).get('arguments') if 'function' in tc else {})

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

                # ✅ FIX: Merge consecutive messages with same role
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

            # ✅ FIX: Use explicit parameters
            temp = temperature if temperature is not None else self.temperature

            # Estimate input tokens (messages + system_content, Gemini sends them separately)
            estimated_input_tokens = count_messages_tokens(messages, self.model_type)
            if system_content:
                # Add system message token estimate
                estimated_input_tokens += count_messages_tokens([{"role": "system", "content": system_content}], self.model_type)

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=estimated_input_tokens,
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
                            # Convert parameters dict to glm.Schema recursively
                            def dict_to_schema(params_dict: Dict) -> glm.Schema:
                                """Recursively convert dict to glm.Schema"""
                                schema_kwargs = {}

                                # Handle type
                                if 'type' in params_dict:
                                    type_map = {
                                        'object': glm.Type.OBJECT,
                                        'string': glm.Type.STRING,
                                        'integer': glm.Type.INTEGER,
                                        'number': glm.Type.NUMBER,
                                        'boolean': glm.Type.BOOLEAN,
                                        'array': glm.Type.ARRAY,
                                    }
                                    schema_kwargs['type'] = type_map.get(params_dict['type'], glm.Type.STRING)

                                # Handle description
                                if 'description' in params_dict:
                                    schema_kwargs['description'] = params_dict['description']

                                # Handle properties (nested schemas)
                                if 'properties' in params_dict:
                                    schema_kwargs['properties'] = {
                                        k: dict_to_schema(v) for k, v in params_dict['properties'].items()
                                    }

                                # Handle required
                                if 'required' in params_dict:
                                    schema_kwargs['required'] = params_dict['required']

                                # Handle items (for arrays)
                                if 'items' in params_dict:
                                    schema_kwargs['items'] = dict_to_schema(params_dict['items'])

                                # Handle enum
                                if 'enum' in params_dict:
                                    schema_kwargs['enum'] = params_dict['enum']

                                return glm.Schema(**schema_kwargs)

                            # Create FunctionDeclaration
                            try:
                                function_declarations.append(
                                    glm.FunctionDeclaration(
                                        name=func['name'],
                                        description=func.get('description', ''),
                                        parameters=dict_to_schema(func.get('parameters', {'type': 'object'}))
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

            # UP-08: opt-in explicit cachedContents for the stable system+tools prefix.
            # When a cache is in use, system_instruction/tools live IN the cache and must
            # NOT be re-sent. Scoped to the non-Gemini-3 path (see _maybe_build_cached_content).
            cached = self._maybe_build_cached_content(system_content, tool_objects)
            if cached is not None:
                model = genai.GenerativeModel.from_cached_content(
                    cached_content=cached,
                    generation_config=generation_config,
                )
            else:
                # Create model with tools (uncached default path)
                model = genai.GenerativeModel(
                    model_name=self.model_type,
                    generation_config=generation_config,
                    tools=tool_objects if tool_objects else None,
                    system_instruction=system_content if system_content else None  # ✅ Issue #5 fix: native support
                )

            # FIX (Dec 2, 2025): Use ChatSession for proper thought signature handling
            # Gemini 3 requires thought signatures for function calling. The SDK's ChatSession
            # handles this automatically by preserving the full response content including signatures.
            # We use start_chat with history for multi-turn conversations.
            # See: https://ai.google.dev/gemini-api/docs/function-calling
            use_chat_session = self._is_gemini_3_model() and tool_objects

            # Log API request details
            func_count = sum(len(t.function_declarations) for t in tool_objects if hasattr(t, 'function_declarations')) if tool_objects else 0
            self.logger.info(
                f"Gemini API call: model={self.model_type}, messages={len(gemini_messages)}, "
                f"functions={func_count}, system={len(system_content) if system_content else 0}chars, "
                f"mode={'ChatSession' if use_chat_session else 'generate_content'}"
            )

            # Debug: Log function names
            if tool_objects and self.logger.isEnabledFor(logging.DEBUG):
                func_names = [f.name for t in tool_objects if hasattr(t, 'function_declarations') for f in t.function_declarations]
                self.logger.debug(f"Available functions: {func_names}")

            # FIX (Dec 2, 2025): Use ChatSession for Gemini 3 to handle thought signatures
            # The SDK's ChatSession automatically preserves thought signatures across turns
            try:
                if use_chat_session:
                    # For Gemini 3 with tools: use ChatSession which handles thought signatures
                    # Split messages into history (all but last) and current message (last)
                    if len(gemini_messages) > 1:
                        history = gemini_messages[:-1]
                        current_message = gemini_messages[-1]
                    else:
                        history = []
                        current_message = gemini_messages[0] if gemini_messages else {"role": "user", "parts": [{"text": ""}]}

                    # Create chat session with history
                    chat = model.start_chat(history=history)

                    # Send the current message - SDK handles thought signatures automatically
                    response = await asyncio.wait_for(
                        chat.send_message_async(current_message["parts"]),
                        timeout=float(self.DEFAULT_REQUEST_TIMEOUT)
                    )
                    self.logger.debug(f"[CHAT_SESSION] Used ChatSession for Gemini 3 thought signature handling")
                else:
                    # Standard path for non-Gemini-3 or no tools
                    response = await asyncio.wait_for(
                        model.generate_content_async(gemini_messages),
                        timeout=float(self.DEFAULT_REQUEST_TIMEOUT)
                    )
            except asyncio.TimeoutError:
                self.logger.error(f"Gemini API call timed out after {self.DEFAULT_REQUEST_TIMEOUT} seconds")
                self.logger.error(f"[DEBUG] Request details: messages={len(gemini_messages)}, tools={len(tool_objects)}, system={bool(system_content)}")
                # Log first message preview
                if gemini_messages:
                    msg_preview = str(gemini_messages[0])[:500]
                    self.logger.error(f"[DEBUG] First message preview: {msg_preview}")

                # Try without tools as fallback
                if tool_objects:
                    self.logger.warning(f"[DEBUG] Retrying without tools to isolate issue...")
                    try:
                        model_no_tools = genai.GenerativeModel(
                            model_name=self.model_type,
                            generation_config=generation_config,
                            system_instruction=system_content if system_content else None
                        )
                        response = await asyncio.wait_for(
                            model_no_tools.generate_content_async(gemini_messages),
                            timeout=float(self.DEFAULT_REQUEST_TIMEOUT)
                        )
                        self.logger.warning(f"[DEBUG] SUCCESS without tools! Tools are the problem.")
                        # Return text-only response as the 3-tuple the caller's
                        # _unpack_tool_gen_result requires (P1 finalization: this
                        # path returned a 2-tuple, raising ValueError downstream and
                        # dropping usage). Stash the response so usage is extracted.
                        text = ""
                        if hasattr(response, 'text'):
                            text = response.text
                        self.last_response = response
                        return text, [], self._extract_usage_data()
                    except Exception as e2:
                        self.logger.error(f"[DEBUG] Also failed without tools: {e2}")

                raise ServiceError(f"Gemini API call timed out after {self.DEFAULT_REQUEST_TIMEOUT} seconds")
            except Exception as e:
                self.logger.error(f"Gemini API call failed with exception: {type(e).__name__}: {e}")
                self.logger.error(f"[DEBUG] Request details: messages={len(gemini_messages)}, tools={len(tool_objects)}, system={bool(system_content)}")
                raise ServiceError(f"Gemini API call failed: {e}")

            self.logger.info(f"[DEBUG] Gemini API call completed successfully")
            
            self.last_response = response
            
            # Check if response contains function calls
            tool_calls = []
            response_text = ""

            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                    for i, part in enumerate(candidate.content.parts):
                        # FIX (Dec 2, 2025): Check for thought_signature on the part itself
                        # Gemini 3 returns thought_signature alongside function_call
                        # We extract and include it in tool_call_data for preservation
                        part_thought_sig = getattr(part, 'thought_signature', None)
                        if part_thought_sig:
                            self.logger.debug(f"[THOUGHT_SIG] Found thought_signature on part {i}: {str(part_thought_sig)[:50]}...")

                        if hasattr(part, 'function_call'):
                            function_call = part.function_call

                            # Extract name with validation
                            name = getattr(function_call, 'name', None)
                            if not name or not name.strip():
                                # ENHANCED DEBUG (Dec 2025): Log full structure to diagnose malformed calls
                                # FIX (Dec 2, 2025): Also log if there's a thought_signature - that helps diagnose
                                self.logger.error(
                                    f"Gemini function_call missing name. "
                                    f"Type: {type(function_call).__name__}, "
                                    f"Dir: {[a for a in dir(function_call) if not a.startswith('_')]}, "
                                    f"Repr: {repr(function_call)[:500]}, "
                                    f"thought_signature_present: {part_thought_sig is not None}"
                                )
                                # Try alternative attribute names that Gemini might use
                                alt_name = (
                                    getattr(function_call, 'function_name', None) or
                                    getattr(function_call, 'tool_name', None) or
                                    getattr(function_call, 'method', None)
                                )
                                if alt_name:
                                    self.logger.info(f"Found alternative name attribute: {alt_name}")
                                    name = alt_name
                                else:
                                    continue  # Skip invalid tool calls

                            # Extract args as dict (avoid JSON serialization)
                            raw_args = getattr(function_call, 'args', None)
                            if raw_args:
                                # Convert protobuf Struct to dict
                                try:
                                    # Struct has .items() like a dict
                                    args_dict = {k: v for k, v in raw_args.items()} if hasattr(raw_args, 'items') else {}
                                except Exception as e:
                                    self.logger.error(f"Failed to convert Gemini args to dict: {e}")
                                    args_dict = {}
                            else:
                                args_dict = {}

                            # FIX (Dec 2, 2025): Include thought_signature in tool call for Gemini 3
                            tool_call_data = {
                                "id": f"gemini_call_{uuid4()}",  # ✅ Issue #1 fix: unique IDs
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": args_dict  # ✅ Issue #2 fix: keep as dict
                                }
                            }

                            # Include thought_signature if present (Gemini 3 requirement)
                            if part_thought_sig:
                                tool_call_data["thought_signature"] = part_thought_sig
                                self.logger.debug(f"[THOUGHT_SIG] Attached to tool call '{name}'")

                            tool_calls.append(tool_call_data)
                        elif hasattr(part, 'text'):
                            response_text += part.text

            # Extract usage data
            usage_data = self._extract_usage_data()

            # Return tool calls if any were found
            if tool_calls:
                # Gemini sometimes returns tool calls without content in native tool mode
                if not response_text or not response_text.strip():
                    self.logger.debug(
                        f"Gemini returned tool calls without content (expected in native tool mode). "
                        f"Tool calls: {[tc['function']['name'] for tc in tool_calls[:3]]}{'...' if len(tool_calls) > 3 else ''}"
                    )
                return response_text, tool_calls, usage_data

            # If no function calls, return text response with empty tool calls list
            if hasattr(response, 'text'):
                return response.text, [], usage_data
            elif hasattr(response, 'parts'):
                text_parts = []
                for part in response.parts:
                    if hasattr(part, 'text') and part.text:
                        text_parts.append(part.text)
                return " ".join(text_parts), [], usage_data

            return "", [], usage_data
            
        except Exception as e:
            self.logger.error(f"Gemini API tool-based generation error: {e}")
            raise ServiceError(f"Gemini API tool-based generation error: {e}")

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from last_response.

        Returns dict with prompt_tokens, completion_tokens, total_tokens.
        Falls back to estimation if usage data not available.
        ✅ Issue #4 fix: Never returns None - uses 0 as last resort.

        Note: Gemini uses usage_metadata with different field names.
        """
        # Initialize defaults
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None
        cached_tokens = 0  # UP-08: Gemini implicit/explicit cache hits

        if not self.last_response:
            self.logger.warning("No last_response available for usage extraction")
        elif hasattr(self.last_response, 'usage_metadata') and self.last_response.usage_metadata:
            # Extract from Gemini usage_metadata
            usage = self.last_response.usage_metadata
            prompt_tokens = getattr(usage, 'prompt_token_count', None)
            completion_tokens = getattr(usage, 'candidates_token_count', None)
            total_tokens = getattr(usage, 'total_token_count', None)
            # UP-08: implicit (2.5+) and explicit cachedContents hits land here.
            cached_tokens = getattr(usage, 'cached_content_token_count', 0) or 0

            # Calculate total if not provided
            if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens

        # Fallback: Estimate from content if no usage data
        if prompt_tokens is None or completion_tokens is None:
            try:
                # Extract text content for estimation
                content_text = ""

                if hasattr(self.last_response, 'text') and self.last_response.text:
                    content_text = self.last_response.text
                elif hasattr(self.last_response, 'candidates') and self.last_response.candidates:
                    # Extract from candidates structure
                    for candidate in self.last_response.candidates:
                        if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                            for part in candidate.content.parts:
                                if hasattr(part, 'text') and part.text:
                                    content_text += part.text

                if content_text:
                    # Rough estimation: ~4 characters per token
                    estimated_output = len(content_text) // 4
                    # Assume input is roughly same size as output
                    estimated_input = estimated_output

                    prompt_tokens = estimated_input if prompt_tokens is None else prompt_tokens
                    completion_tokens = estimated_output if completion_tokens is None else completion_tokens
                    total_tokens = prompt_tokens + completion_tokens

                    self.logger.info(f"Estimated Gemini tokens: {total_tokens} (usage_metadata unavailable)")
            except Exception as e:
                self.logger.debug(f"Failed to estimate tokens: {e}")

        # Final validation: Never return None - use 0 as last resort
        usage_data = {
            'prompt_tokens': prompt_tokens if prompt_tokens is not None else 0,
            'completion_tokens': completion_tokens if completion_tokens is not None else 0,
            'total_tokens': total_tokens if total_tokens is not None else 0,
            'cached_tokens': cached_tokens,
        }

        if usage_data['total_tokens'] == 0:
            self.logger.warning("No usage data available and estimation failed - using 0 tokens")

        self.logger.debug(f"Extracted usage: {usage_data}")
        return usage_data 