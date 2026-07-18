"""LLM adapters for custom LLM clients.

This module provides adapters to convert our custom LLM clients into
a consistent interface for use with the agents system.

REFACTORED (Nov 25, 2025):
- Consolidated vision support detection to use model_registry as single source of truth
- Added provider-specific image format conversion (Anthropic requires different format)
- Removed redundant keyword-based model detection

REFACTORED (Dec 12, 2025):
- Removed third-party agent-framework dependency (now fully native)
- Now uses native message types from modules.llm.messages
"""

import logging
import asyncio
import os
from typing import Any, Dict, List, Optional, Union
from abc import ABC, abstractmethod

# Native message types
from modules.llm.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
    ChatGeneration, ChatResult
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# WS revalidation (2026-07-16): the four provider-client imports are TYPE_CHECKING-only.
# They were the eager chain pulling the anthropic/openai/google.generativeai SDKs into
# EVERY entry-point import (cli.polyrob, api.app) via agents.task.agent -> service ->
# adapters — the client classes are used here only as __init__ annotations; the real
# instances are constructed by llm_factory, which imports the client modules lazily.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from modules.llm.anthropic_client import AnthropicClient
    from modules.llm.deepseek_client import DeepSeekClient
    from modules.llm.gemini_client import GeminiClient
    from modules.llm.openai_client import OpenAIClient
from modules.llm.llm_client import LLMClient, translate_llm_error
from modules.llm.model_registry import get_model_config


def think_scrubber_enabled() -> bool:
    """UP-07 gate for the reasoning-block scrubber. Default ON, fail-open.

    Disable with THINK_SCRUBBER_ENABLED in {0, false, no, off}. Mirrors
    cache_hints.prompt_cache_enabled() semantics.
    """
    return os.getenv("THINK_SCRUBBER_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def token_streaming_enabled() -> bool:
    """019 P5 gate for TRUE per-token streaming. Default OFF.

    When ON and the wrapped client implements ``astream_agent_response``,
    ``LLMClientAdapter.astream`` yields real deltas instead of the legacy
    single full-response chunk. OFF = byte-identical legacy behavior.
    """
    return os.getenv("LLM_TOKEN_STREAMING", "0").lower() in ("1", "true", "yes", "on")


class BaseChatModel(BaseModel, ABC):
    """Abstract base class for chat models.

    This is POLYROB's native BaseChatModel (no third-party dependency).
    Provides the essential interface for chat model adapters.
    """
    model_name: str = Field(default="unknown")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @abstractmethod
    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, **kwargs) -> ChatResult:
        """Generate a response asynchronously."""
        pass

    @abstractmethod
    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, **kwargs) -> ChatResult:
        """Generate a response synchronously."""
        pass

    @property
    @abstractmethod
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        pass

    def prepare_cache_hints(self, messages: List[BaseMessage], tools: Optional[List[Any]] = None) -> dict:
        """Provider-agnostic prompt-cache seam (P1-3).

        Returns a dict of cache hints for the concrete adapter/client to apply when
        assembling the request (e.g. which prefix blocks to mark cacheable). The base
        implementation is a no-op (``{}``) so providers that handle caching in-client
        (Anthropic, OpenAI) or have none are unaffected. The canonical policy lives in
        ``modules.llm.cache_hints``; adapters that opt in consult it from here.
        """
        return {}

    async def ainvoke(self, input, config=None, *, stop=None, **kwargs) -> AIMessage:
        """Invoke the model asynchronously."""
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        result = await self._agenerate(messages, stop=stop, **kwargs)
        if result.generations:
            return result.generations[0].message
        return AIMessage(content="")

    def invoke(self, input, config=None, *, stop=None, **kwargs) -> AIMessage:
        """Invoke the model synchronously."""
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        result = self._generate(messages, stop=stop, **kwargs)
        if result.generations:
            return result.generations[0].message
        return AIMessage(content="")


class LLMClientAdapter(BaseChatModel):
    """Base adapter class for converting our LLM clients to a consistent interface.

    Native LLM-client adapter.
    The old name is preserved as an alias for backward compatibility.
    """

    # Use Pydantic's PrivateAttr for client to avoid validation issues
    _client: LLMClient = PrivateAttr(default=None)
    _logger: logging.Logger = PrivateAttr(default=None)
    # Cached vision-support flag (lazily filled by _get_vision_support). MUST be a
    # declared PrivateAttr — Pydantic v2 raises AttributeError on an undeclared
    # private attr, which previously crashed the first multimodal request.
    _supports_vision_cached: Optional[bool] = PrivateAttr(default=None)
    # Configured generation defaults from create_chat_model. These MUST be captured
    # here: they arrive as constructor kwargs but BaseChatModel declares no such
    # field, so Pydantic (extra='ignore') would silently drop them — the requested
    # temperature/max_tokens would never reach the API and every call would use the
    # client's hardcoded default (0.7). Used as the generation fallback in _agenerate.
    _default_temperature: Optional[float] = PrivateAttr(default=None)
    _default_max_tokens: Optional[int] = PrivateAttr(default=None)

    # Declare model_name as a proper field
    model_name: str = Field(default="unknown")

    def __init__(self, client: LLMClient, **kwargs):
        """Initialize with an LLM client.

        Args:
            client: An initialized LLM client
        """
        # Capture the configured generation params before super().__init__ drops them
        # (Pydantic ignores extras). These become the per-call fallback so a requested
        # temperature=0.0 is actually honored instead of silently reverting to 0.7.
        default_temperature = kwargs.pop("temperature", None)
        default_max_tokens = kwargs.pop("max_tokens", None)

        # Set model_name before calling super().__init__ so Pydantic can validate it
        # FIXED: Only use client.model_type as fallback if model_name not already provided
        # This allows webview model selection to work - don't overwrite the passed model_name!
        if "model_name" not in kwargs:
            kwargs["model_name"] = getattr(client, "model_type", "unknown")
        super().__init__(**kwargs)

        # Set private attributes
        self._client = client
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._logger = logging.getLogger(f"{self.__class__.__name__}")

    def _scrub_content(self, content):
        """Strip leaked reasoning blocks from assistant text (UP-07).

        The single provider-agnostic seam: every provider's content passes through here
        before becoming an AIMessage, so a leaked <think>/<reasoning>/... block never
        reaches history, brain-state extraction, or the user stream. Gated by
        THINK_SCRUBBER_ENABLED (default ON, fail-open). Touches str content only —
        tool_calls/usage/multimodal content are never modified. The no-'<' fast path
        leaves the overwhelming majority of completions untouched.
        """
        if not content or not isinstance(content, str) or not think_scrubber_enabled():
            return content
        if "<" not in content:
            return content
        try:
            from modules.llm.think_scrubber import scrub_think_blocks
            scrubbed = scrub_think_blocks(content)
        except Exception as e:  # never break generation on a scrub bug
            self._logger.debug(f"think scrub skipped (error): {e}")
            return content
        if scrubbed != content:
            self._logger.warning(
                f"event=think_block_scrub model={getattr(self._client, 'model_type', '?')} "
                f"removed_chars={len(content) - len(scrubbed)} "
                f"(stripped leaked reasoning block from assistant content)"
            )
        return scrubbed

    @staticmethod
    def _unpack_tool_gen_result(result):
        """Unpack a provider's generate_agent_response() return value.

        Contract: tool-generation returns (content: str|None, tool_calls: list, usage: dict).
        All providers MUST return this 3-tuple; any other shape is a contract violation.

        Raises:
            ValueError: if `result` is not a 3-tuple (enforces the contract going forward).
        """
        if not isinstance(result, tuple) or len(result) != 3:
            raise ValueError(
                f"Tool-generation contract violation: expected a 3-tuple "
                f"(content, tool_calls, usage) but got {type(result).__name__} "
                f"of length {len(result) if isinstance(result, tuple) else 'N/A'}: {result!r}"
            )
        return result

    def _get_vision_support(self) -> bool:
        """Get vision support from model_registry (single source of truth).
        
        REFACTORED (Nov 25, 2025): Removed redundant keyword matching.
        Now uses model_registry.get_model_config() as the canonical source.
        
        Falls back to client.supports_vision attribute if registry lookup fails.
        
        Returns:
            bool: True if model supports vision/multimodal content
        """
        # Return cached value if available
        if self._supports_vision_cached is not None:
            return self._supports_vision_cached
        
        # Try model registry first (canonical source)
        model_type = getattr(self._client, 'model_type', None)
        if model_type:
            model_config = get_model_config(model_type)
            if model_config and hasattr(model_config, 'capabilities'):
                self._supports_vision_cached = model_config.capabilities.supports_vision
                self._logger.debug(
                    f"Vision support from registry: {model_type} -> {self._supports_vision_cached}"
                )
                return self._supports_vision_cached
        
        # Fallback to client attribute (set by individual clients from registry)
        supports_vision = getattr(self._client, 'supports_vision', False)
        self._supports_vision_cached = supports_vision
        self._logger.debug(f"Vision support from client attribute: {supports_vision}")
        return supports_vision
        
    def _convert_to_client_messages(self, messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        """Convert messages to client-compatible format.
        
        Args:
            messages: List of messages
            
        Returns:
            List of messages in client-compatible format
        """
        client_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                role = "system"
            elif isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, ToolMessage):
                # Preserve tool message with proper fields
                role = "tool"
                tool_msg = {"role": role, "content": msg.content}
                if hasattr(msg, 'tool_call_id'):
                    tool_msg["tool_call_id"] = msg.tool_call_id
                client_messages.append(tool_msg)
                continue
            elif isinstance(msg, AIMessage):
                role = "assistant"
                # Check if AIMessage has tool_calls
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    assistant_msg = {"role": role, "content": msg.content or ""}
                    assistant_msg["tool_calls"] = msg.tool_calls
                    client_messages.append(assistant_msg)
                    continue
            else:
                role = "assistant"

            # Handle text vs. multimodal content for non-tool messages
            if isinstance(msg.content, str):
                client_messages.append({"role": role, "content": msg.content})
            elif isinstance(msg.content, list):
                # T-05: Preserve multimodal content for vision-capable models
                # REFACTORED (Nov 25, 2025): Use model_registry as single source of truth
                # instead of redundant keyword matching
                supports_vision = self._get_vision_support()

                # Enhanced logging for vision detection
                image_count = sum(1 for p in msg.content if isinstance(p, dict) and p.get('type') == 'image_url')
                if image_count > 0:
                    self._logger.info(
                        f"📷 Vision detection: supports_vision={supports_vision}, "
                        f"model_type={getattr(self._client, 'model_type', 'unknown')}, "
                        f"images={image_count}"
                    )
                    if not supports_vision:
                        self._logger.warning(
                            f"⚠️ Vision support=False for {getattr(self._client, 'model_type', 'unknown')} "
                            f"but {image_count} image(s) present. Images will be replaced with '[IMAGE]'."
                        )

                if supports_vision:
                    # Preserve full multimodal content
                    content_parts = []
                    for item in msg.content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                content_parts.append({"type": "text", "text": item.get("text", "")})
                            elif item.get("type") == "image_url":
                                # Preserve image URLs/base64 data
                                content_parts.append(item)
                            elif item.get("type") == "image":
                                # Alternative image format
                                content_parts.append(item)
                    client_messages.append({"role": role, "content": content_parts})
                else:
                    # Fallback: extract only text for non-vision models
                    processed_content = []
                    for item in msg.content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            processed_content.append(item.get("text", ""))
                        elif isinstance(item, dict) and item.get("type") == "image_url":
                            # Add placeholder for images in non-vision models
                            processed_content.append("[IMAGE]")

                    client_messages.append({"role": role, "content": " ".join(processed_content)})
                
        return client_messages
    
    def _build_generation_params(self, stop: Optional[List[str]], kwargs: dict) -> dict:
        """Assemble per-call generation params (shared by _agenerate + _astream_true).

        Prefers an explicit per-call value; otherwise falls back to the
        configured default captured at construction (so the temperature/
        max_tokens passed to create_chat_model is actually honored). Also
        folds telemetry metadata (session/agent ids) into the params.
        """
        # Extract metadata for telemetry - look for session and agent information
        metadata = {}
        if 'metadata' in kwargs:
            metadata.update(kwargs['metadata'])
        if 'session_id' in kwargs:
            metadata['session_id'] = kwargs['session_id']
        elif 'run_id' in kwargs:
            # Some adapters use run_id
            metadata['session_id'] = kwargs['run_id']
        if 'agent_id' in kwargs:
            metadata['agent_id'] = kwargs['agent_id']

        generation_params = {}
        temperature = kwargs.get("temperature")
        if temperature is None:
            temperature = self._default_temperature
        if temperature is not None:
            generation_params["temperature"] = temperature

        max_tokens = kwargs.get("max_tokens")
        if max_tokens is None:
            max_tokens = self._default_max_tokens
        if max_tokens is not None:
            generation_params["max_tokens"] = max_tokens

        if stop:
            generation_params["stop_sequences"] = stop

        if metadata:
            generation_params["metadata"] = metadata
        return generation_params

    @staticmethod
    def _usage_metadata_from(usage_data: Optional[dict]) -> Optional[dict]:
        """Map a client usage_data dict to the standard usage_metadata shape
        (shared by _agenerate + _astream_true so accounting can't diverge)."""
        if not usage_data:
            return None
        usage_metadata = {
            'input_tokens': usage_data.get('prompt_tokens'),
            'output_tokens': usage_data.get('completion_tokens'),
            'total_tokens': usage_data.get('total_tokens'),
        }
        # Add cached tokens if present
        if usage_data.get('cached_tokens'):
            usage_metadata['cache_read_input_tokens'] = usage_data.get('cached_tokens')
        # G3: cache-WRITE (creation) tokens, billed at a surcharge downstream
        if usage_data.get('cache_creation_tokens'):
            usage_metadata['cache_creation_input_tokens'] = usage_data.get('cache_creation_tokens')
        return usage_metadata

    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, **kwargs) -> ChatResult:
        """Generate a response asynchronously.

        Args:
            messages: List of messages
            stop: Optional stop sequences
            kwargs: Additional parameters for generation

        Returns:
            ChatResult with generations
        """
        import time
        start_time = time.time()

        self._logger.debug(f"_agenerate called:")
        self._logger.debug(f"  Model type: {self._client.model_type}")
        self._logger.debug(f"  Messages count: {len(messages)}")
        self._logger.debug(f"  Message types: {[type(m).__name__ for m in messages]}")
        self._logger.debug(f"  Has stop sequences: {bool(stop)}")
        self._logger.debug(f"  Has tools: {'tools' in kwargs}")
        if 'tools' in kwargs:
            tools = kwargs.get('tools', [])
            self._logger.debug(f"  Tool count: {len(tools) if tools else 0}")
            if tools:
                # Log first few tool names - ensure tools is a list for safe slicing
                tool_names = []
                tools_list = list(tools) if not isinstance(tools, list) else tools
                for t in tools_list[:5]:
                    if isinstance(t, dict) and 'function' in t:
                        tool_names.append(t['function'].get('name', 'unknown'))
                    else:
                        tool_names.append(str(t)[:20])
                self._logger.debug(f"  Tool names (first 5): {tool_names}")

        self._logger.debug(f"Generating response with base model {self._client.model_type}")

        # Convert to client-compatible format
        self._logger.debug(f"Converting {len(messages)} messages to client format")
        client_messages = self._convert_to_client_messages(messages)
        self._logger.debug(f"Converted to {len(client_messages)} client messages")

        generation_params = self._build_generation_params(stop, kwargs)

        # Check if tools are provided - with fallback to _pending_tools
        # CRITICAL FIX (Dec 2025): the adapter's internal routing may bypass our
        # ainvoke override and call _agenerate directly without kwargs.
        # The _pending_tools fallback ensures tools are available in all cases.
        tools = kwargs.get('tools')
        if not tools and hasattr(self, '_pending_tools') and self._pending_tools:
            tools = self._pending_tools
            self._logger.info(f"[TOOLS_FIX] Retrieved {len(tools)} tools from _pending_tools fallback")
            # Clear after use to prevent stale data
            self._pending_tools = None
        
        self._logger.info(f"[DEBUG_TOOLS] Adapter extracted tools from kwargs: {len(tools) if tools else 0}")
        self._logger.info(f"[DEBUG_TOOLS] Client has generate_agent_response: {hasattr(self._client, 'generate_agent_response')}")

        # Generate response
        try:
            # Check if native tool path is available and should be used
            native_tool_path_available = tools and hasattr(self._client, 'generate_agent_response')
            self._logger.info(f"[DEBUG_TOOLS] Native tool path available: {native_tool_path_available}")

            if native_tool_path_available:
                # Use agent response method for tool calling
                self._logger.debug(f"[NATIVE_TOOLS] Using native tool-calling path")
                self._logger.debug(f"Calling client.generate_agent_response with tools")
                self._logger.debug(f"  Client class: {self._client.__class__.__name__}")
                self._logger.debug(f"  Messages: {len(client_messages)}")
                self._logger.debug(f"  Tools: {len(tools)}")

                client_start = time.time()
                response = await self._client.generate_agent_response(
                    messages=client_messages,
                    tools=tools,
                    **generation_params
                )
                client_duration = time.time() - client_start

                self._logger.debug(f"Client.generate_agent_response completed in {client_duration:.1f}s")
                self._logger.debug(f"Response type: {type(response).__name__}")

                # Unpack the canonical 3-tuple: (content, tool_calls, usage_data).
                # Contract: all providers MUST return this shape from generate_agent_response().
                # _unpack_tool_gen_result() enforces the contract and raises on any other shape.
                content, tool_calls, usage_data = self._unpack_tool_gen_result(response)
                self._logger.debug(f"[NATIVE_TOOLS] Received 3-tuple with usage data: {usage_data}")

                # Clients may return tool calls without content in native tool mode
                if tool_calls and (not content or not content.strip()):
                    self._logger.debug(
                        f"[NATIVE_TOOLS] Client returned {len(tool_calls)} tool calls without content "
                        f"(expected in native tool mode)"
                    )

                # Create AIMessage with tool calls and usage data
                # CRITICAL: Populate usage_metadata in standard format
                # so extract_token_usage() can find it
                usage_metadata = self._usage_metadata_from(usage_data)
                if usage_metadata:
                    self._logger.debug(f"[NATIVE_TOOLS] Populating usage_metadata for token extraction: {usage_metadata}")

                ai_message = AIMessage(content=self._scrub_content(content or ""), usage_metadata=usage_metadata)
                if tool_calls:
                    ai_message.tool_calls = tool_calls
                    self._logger.debug(f"[NATIVE_TOOLS] Response contains {len(tool_calls)} tool calls, content_length={len(content) if content else 0}")
                else:
                    self._logger.debug(f"[NATIVE_TOOLS] Response contains no tool calls (content only)")
                generation = ChatGeneration(message=ai_message)
            else:
                # Log why native tools path is NOT being used
                if tools and not hasattr(self._client, 'generate_agent_response'):
                    self._logger.warning(f"[NATIVE_TOOLS] Tools provided but client {self._client.__class__.__name__} lacks generate_agent_response method")
                elif not tools:
                    self._logger.info(f"[NATIVE_TOOLS] No tools provided, using standard generation")

                # Standard generation without tools
                self._logger.info(f"[DEBUG] Calling client.generate_response (no tools)")
                self._logger.info(f"[DEBUG]   Client class: {self._client.__class__.__name__}")
                self._logger.info(f"[DEBUG]   Messages: {len(client_messages)}")

                client_start = time.time()
                response = await self._client.generate_response(
                    messages=client_messages,
                    **generation_params
                )
                client_duration = time.time() - client_start

                self._logger.info(f"[DEBUG] Client.generate_response completed in {client_duration:.1f}s")
                self._logger.info(f"[DEBUG] Response type: {type(response).__name__}")

                # Convert to flat format
                ai_message = AIMessage(content=self._scrub_content(response))
                generation = ChatGeneration(message=ai_message)

            # Get the raw response with token usage if available
            raw_response = getattr(self._client, 'last_response', None)

            # Add raw response data for token extraction
            if raw_response:
                generation.generation_info = {"raw": raw_response}

                # Money-correctness fix (Task 5c fix pass 2, reviewer rec (a)):
                # stamp the provider's own response id onto THIS per-call
                # `ai_message` object (never the shared `self._client`) right
                # here, synchronously, with no intervening await since the
                # client call above returned. Under default-on parallel
                # sub-agent delegation, multiple concurrent Agent.run() loops
                # can share the SAME LLM client object (SubAgentManager.
                # run_subtask inherits `parent_agent.llm` verbatim when a
                # subtask has no own model), so `self._client.last_response`
                # is a single mutable slot two concurrent completions race to
                # overwrite -- reading it later (e.g. in billing) can pick up
                # ANOTHER call's id. Stamping it onto the per-call response
                # object closes that window: each concurrent caller gets its
                # own AIMessage with its own id, immune to the shared slot
                # being overwritten afterwards. Covers BOTH branches above
                # (native tool-calling and the plain path) since they
                # converge here before either `ai_message`/`generation` is
                # returned. Absent on providers whose raw response carries no
                # id (e.g. Gemini) -- `extract_stable_request_id` then falls
                # back to the old shared-client read and finally to a fresh
                # uuid, never a false dedup.
                provider_response_id = (
                    raw_response.get('id') if isinstance(raw_response, dict)
                    else getattr(raw_response, 'id', None)
                )
                if isinstance(provider_response_id, str) and provider_response_id:
                    ai_message._polyrob_provider_response_id = provider_response_id

            total_duration = time.time() - start_time
            self._logger.debug(f"_agenerate completed in {total_duration:.1f}s")
            self._logger.debug(f"Returning ChatResult with 1 generation")

            return ChatResult(generations=[generation])

        except Exception as e:
            from core.exceptions import (
                LLMError, LLMRateLimitError, LLMAuthenticationError,
                LLMConnectionError, LLMContextLengthError, LLMResponseError,
                LLMPermanentError
            )

            # CRITICAL FIX: Propagate LLM-specific exceptions instead of swallowing them.
            # This allows the task agent to catch these and try fallback providers.
            if isinstance(e, (LLMRateLimitError, LLMAuthenticationError,
                              LLMConnectionError, LLMContextLengthError,
                              LLMResponseError, LLMPermanentError, LLMError)):
                self._logger.warning(f"LLM error (propagating for fallback): {type(e).__name__}: {str(e)[:200]}")
                raise

            # Route through the single unified classifier (translate_llm_error).
            # Preserves all previous categories including billing→LLMPermanentError.
            provider_name = self._client.__class__.__name__
            translated = translate_llm_error(e, f"from {provider_name}")
            if isinstance(translated, LLMPermanentError):
                self._logger.error(f"Detected PERMANENT error (no fallback): {str(e)[:200]}")
            elif isinstance(translated, LLMRateLimitError):
                self._logger.warning(f"Detected rate limit error: {str(e)[:200]}")
            elif isinstance(translated, LLMAuthenticationError):
                self._logger.warning(f"Detected authentication error: {str(e)[:200]}")
            elif isinstance(translated, (LLMContextLengthError, LLMConnectionError)):
                self._logger.warning(f"Detected {type(translated).__name__}: {str(e)[:200]}")
            else:
                self._logger.error(f"Unexpected LLM error (propagating): {type(e).__name__}: {str(e)}")
            raise translated
    
    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, **kwargs) -> ChatResult:
        """Generate a response synchronously.
        
        Args:
            messages: List of messages
            stop: Optional stop sequences
            kwargs: Additional parameters for generation
            
        Returns:
            ChatResult with generations
        """
        # T-06: Better asyncio handling with nest_asyncio
        try:
            import nest_asyncio
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're already in an async context, schedule the coroutine
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self._agenerate(messages, stop, **kwargs))
                    return future.result()
            else:
                return loop.run_until_complete(self._agenerate(messages, stop, **kwargs))
        except ImportError:
            # Fallback if nest_asyncio not available
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create task in current loop
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self._agenerate(messages, stop, **kwargs))
                    return future.result()
            else:
                return loop.run_until_complete(self._agenerate(messages, stop, **kwargs))
    
    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return "llm-client-adapter"

    async def ainvoke(self, input, config=None, *, stop=None, **kwargs) -> AIMessage:
        """Override ainvoke to pass tools through kwargs to _agenerate.

        The base ainvoke doesn't pass arbitrary kwargs to _agenerate.
        This override ensures tools are passed through for native function calling.
        
        CRITICAL FIX (Dec 2025): Also store tools in instance variable as fallback
        since internal routing may bypass this method in some cases.
        """
        # Convert input to messages if needed
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        # CRITICAL FIX: Store tools in instance variable as fallback mechanism
        # This ensures _agenerate can access tools even if the adapter's routing
        # bypasses this method and calls _agenerate directly
        if 'tools' in kwargs and kwargs['tools']:
            self._pending_tools = kwargs['tools']
            self._logger.debug(f"[TOOLS_FIX] Stored {len(kwargs['tools'])} tools in _pending_tools")
        
        # Call _agenerate with all kwargs (including tools)
        result = await self._agenerate(messages, stop=stop, **kwargs)

        # Extract the AIMessage from ChatResult
        if result.generations:
            return result.generations[0].message
        return AIMessage(content="")

    async def astream(self, input, config=None, *, stop=None, **kwargs):
        """Stream a response, passing tools through kwargs.

        Default (legacy, byte-identical): the full ainvoke() result wrapped as
        a 1-item async iterator — "streaming" of one chunk.

        019 P5 (`LLM_TOKEN_STREAMING`, default OFF): when the flag is ON and
        the wrapped client implements ``astream_agent_response``, yields TRUE
        per-token deltas via ``_astream_true``. A failure BEFORE the first
        yielded chunk falls back to the legacy single-chunk form; after the
        first chunk it propagates (the agent loop's retry machinery owns it —
        a silent fallback would double the streamed text).

        CRITICAL FIX (Dec 2025): Also store tools in instance variable as fallback.
        """
        # CRITICAL FIX: Store tools in instance variable before calling ainvoke
        if 'tools' in kwargs and kwargs['tools']:
            self._pending_tools = kwargs['tools']
            self._logger.debug(f"[TOOLS_FIX] astream: Stored {len(kwargs['tools'])} tools in _pending_tools")

        if token_streaming_enabled() and hasattr(self._client, 'astream_agent_response'):
            yielded_any = False
            try:
                async for chunk in self._astream_true(input, stop=stop, **kwargs):
                    yielded_any = True
                    yield chunk
                return
            except Exception as e:
                if yielded_any:
                    raise
                self._logger.warning(
                    f"true token streaming failed before first chunk; "
                    f"falling back to single-chunk: {type(e).__name__}: {e}"
                )

        # Legacy: yield the full response as a single chunk
        response = await self.ainvoke(input, config=config, stop=stop, **kwargs)
        yield response

    async def _astream_true(self, input, *, stop=None, **kwargs):
        """TRUE token streaming (019 P5): scrubbed text deltas, then ONE final
        AIMessage carrying tool_calls / usage_metadata / provider-response-id.

        Contract with the client's ``astream_agent_response``: it yields
        ``{"type": "text", "text": str}`` deltas followed by exactly one
        ``{"type": "final", "content", "tool_calls", "usage_data",
        "response_id"}``. Consumers that concatenate chunk content reconstruct
        exactly the scrubbed completion the non-streaming path would return —
        the final chunk carries only the UN-streamed tail (or the whole content
        when deltas were suppressed).

        Reasoning scrub: deltas run through a per-call StreamingThinkScrubber
        (same algorithm the whole-string scrub uses), so a ``<think>`` block
        split across delta boundaries never leaks.

        Brain-state guard (three layers): a native-tools completion usually
        BEGINS with the brain-state JSON — streaming that would show raw JSON
        fragments to the user. (1) The stream is held until the first
        non-whitespace character: ``{`` OR a backtick (fenced ```json payloads)
        suppresses live deltas for the whole call — the final chunk then
        carries the full content, exact legacy single-chunk behavior. (2) Once
        live, a rolling window watches for the brain-state marker
        (``"current_state"`` — e.g. prose followed by a TRAILING brain block);
        on sight the stream MUTES: no further live deltas, the remainder rides
        the final chunk as one piece, where the downstream whole-chunk brain
        scrub works. (3) hitl_manager's per-chunk scrub stays as backstop.
        """
        # Convert input to messages (mirrors ainvoke)
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        client_messages = self._convert_to_client_messages(messages)
        generation_params = self._build_generation_params(stop, kwargs)
        tools = kwargs.get('tools')
        if not tools and getattr(self, '_pending_tools', None):
            tools = self._pending_tools
            self._pending_tools = None

        scrubber = None
        if think_scrubber_enabled():
            try:
                from modules.llm.think_scrubber import StreamingThinkScrubber
                scrubber = StreamingThinkScrubber()
            except Exception:
                scrubber = None

        mode = "holding"  # -> "live" | "suppressed" | "muted"
        held = ""
        window = ""  # rolling tail of LIVE-emitted text for cross-delta marker detection
        muted_parts: List[str] = []  # scrubbed text withheld after a live->muted flip
        final_event = None
        _BRAIN_MARKER = '"current_state"'

        async for event in self._client.astream_agent_response(
            messages=client_messages, tools=tools, **generation_params
        ):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "text":
                piece = event.get("text") or ""
                if scrubber is not None:
                    try:
                        piece = scrubber.feed(piece)
                    except Exception:
                        scrubber = None  # fail-open: stream raw from here on
                        piece = event.get("text") or ""
                if not piece:
                    continue
                if mode == "holding":
                    held += piece
                    stripped = held.lstrip()
                    if not stripped:
                        continue
                    # '{' = brain-state JSON; '`' = fenced payload (```json …)
                    if stripped[0] in "{`":
                        mode = "suppressed"
                    else:
                        mode = "live"
                        if _BRAIN_MARKER in held:
                            mode = "muted"
                            muted_parts.append(held)
                        else:
                            yield AIMessage(content=held)
                            window = held[-32:]
                        held = ""
                elif mode == "live":
                    if _BRAIN_MARKER in window + piece:
                        # a trailing brain block began mid-stream — stop live
                        # emission; the remainder rides the final chunk whole.
                        mode = "muted"
                        muted_parts.append(piece)
                    else:
                        yield AIMessage(content=piece)
                        window = (window + piece)[-32:]
                elif mode == "muted":
                    muted_parts.append(piece)
                # suppressed: swallow — the final chunk carries everything
            elif event.get("type") == "final":
                final_event = event

        if final_event is None:
            raise RuntimeError(
                "streaming client ended without a final event "
                f"({self._client.__class__.__name__}.astream_agent_response)"
            )

        tail = ""
        if scrubber is not None:
            try:
                tail = scrubber.flush()
            except Exception:
                tail = ""
        if mode == "live":
            final_content = tail
        elif mode == "muted":
            # everything withheld since the flip + the scrubber tail, as ONE
            # piece — consumers concatenating chunk content still reconstruct
            # the full completion, and the downstream whole-chunk brain scrub
            # sees the complete block.
            final_content = "".join(muted_parts) + tail
        else:
            # holding (empty/whitespace-only stream) or suppressed: the final
            # chunk carries the WHOLE scrubbed completion, legacy-equivalent.
            final_content = self._scrub_content(final_event.get("content") or "")

        final_message = AIMessage(
            content=final_content,
            usage_metadata=self._usage_metadata_from(final_event.get("usage_data")),
        )
        tool_calls = final_event.get("tool_calls")
        if tool_calls:
            final_message.tool_calls = tool_calls
        response_id = final_event.get("response_id")
        if isinstance(response_id, str) and response_id:
            final_message._polyrob_provider_response_id = response_id
        yield final_message


class DeepSeekAdapter(LLMClientAdapter):
    """Adapter for DeepSeek client with native function calling support.

    DeepSeek V3+ supports OpenAI-compatible function calling via generate_agent_response.
    The base class _agenerate automatically routes to native tools when available.

    Docs: https://api-docs.deepseek.com/guides/function_calling
    """

    def __init__(self, client: "DeepSeekClient", **kwargs):
        """Initialize with a DeepSeek client.

        Args:
            client: An initialized DeepSeek client
        """
        super().__init__(client, **kwargs)

    # ✅ NATIVE TOOLS: Base class _agenerate (line 117) automatically calls
    # client.generate_agent_response with tools, which DeepSeekClient implements
    # via _generate_with_tools for OpenAI-compatible function calling

    def with_structured_output(self, schema, **kwargs):
        """DeepSeek doesn't support schema-based structured output.

        DeepSeek V3 supports:
        - ✅ Function calling (native tools) - handled by generate_agent_response
        - ❌ Structured output (schema-based response formatting)

        This method raises NotImplementedError to trigger fallback to regular
        LLM calls with JSON parsing, which is the correct fallback for DeepSeek.

        Note: Function calling and structured output are different capabilities.
        The agent will use native tools successfully, but skip structured output.
        """
        raise NotImplementedError(
            f"Structured output not supported for DeepSeek model {self._client.model_type}. "
            "Use function calling (native tools) or regular LLM calls with JSON parsing."
        )

    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"deepseek-{self._client.model_type}-adapter"


class GeminiAdapter(LLMClientAdapter):
    """Adapter for Gemini client - uses base class native tools implementation.
    
    IMPORTANT: Gemini accepts OpenAI-style image_url format and handles conversion
    internally in GeminiClient._generate_with_tools(). No adapter-level conversion needed.
    """

    def __init__(self, client: "GeminiClient", **kwargs):
        """Initialize with a Gemini client.

        Args:
            client: An initialized Gemini client
        """
        super().__init__(client, **kwargs)

    # ✅ Gemini handles image format conversion internally in _generate_with_tools
    # The GeminiClient converts image_url to Gemini's Part format when processing messages

    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"gemini-{self._client.model_type}-adapter"


class DeepSeekAgentAdapter(DeepSeekAdapter):
    """Specialized adapter for DeepSeek models to work with Agent's structured output.
    
    This adapter handles the issue where DeepSeek models have trouble with 
    schema-based structured output parsing when the output model is not "strict".
    Instead, it implements manual JSON parsing to extract the required structure.
    """
    
    def __init__(self, client: "DeepSeekClient", output_schema_class: Any = None, **kwargs):
        """Initialize with a DeepSeek client and optional output schema.
        
        Args:
            client: An initialized DeepSeek client
            output_schema_class: The output schema class (e.g., AgentOutput)
            **kwargs: Additional parameters
        """
        super().__init__(client, **kwargs)
        self._output_schema_class = output_schema_class
        self._logger = logging.getLogger(f"{self.__class__.__name__}")
    
    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"deepseek-agent-adapter-{self._client.model_type}"


class GeminiAgentAdapter(GeminiAdapter):
    """Specialized adapter for Gemini models to work with Agent's structured output.
    
    This adapter handles structured output parsing for agent interactions,
    implementing manual JSON parsing when needed to extract the required structure.
    """
    
    def __init__(self, client: "GeminiClient", output_schema_class: Any = None, **kwargs):
        """Initialize with a Gemini client and optional output schema.
        
        Args:
            client: An initialized Gemini client
            output_schema_class: The output schema class (e.g., AgentOutput)
            **kwargs: Additional parameters
        """
        super().__init__(client, **kwargs)
        self._output_schema_class = output_schema_class
        self._logger = logging.getLogger(f"{self.__class__.__name__}")
    
    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"gemini-agent-adapter-{self._client.model_type}"


class OpenAIAdapter(LLMClientAdapter):
    """Adapter for OpenAI client - provides consistent pattern across all providers."""
    
    def __init__(self, client: "OpenAIClient", **kwargs):
        """Initialize with an OpenAI client.
        
        Args:
            client: An initialized OpenAI client
        """
        super().__init__(client, **kwargs)
        
    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"openai-{self._client.model_type}-adapter"


class AnthropicAdapter(LLMClientAdapter):
    """Adapter for Anthropic client - provides consistent pattern across all providers.
    
    CRITICAL: Anthropic uses a different image format than OpenAI.
    This adapter converts OpenAI's image_url format to Anthropic's native image format.
    
    OpenAI format:
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    
    Anthropic format:
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    """
    
    def __init__(self, client: "AnthropicClient", **kwargs):
        """Initialize with an Anthropic client.
        
        Args:
            client: An initialized Anthropic client
        """
        super().__init__(client, **kwargs)
        
    def _convert_image_to_anthropic_format(self, image_item: Dict[str, Any]) -> Dict[str, Any]:
        """Convert OpenAI image_url format to Anthropic image format.
        
        Args:
            image_item: Image in OpenAI format {"type": "image_url", "image_url": {"url": "..."}}
            
        Returns:
            Image in Anthropic format {"type": "image", "source": {...}}
        """
        try:
            image_url_data = image_item.get("image_url", {})
            url = image_url_data.get("url", "")
            
            if not url:
                self._logger.warning("Empty image URL in image_url format")
                return {"type": "text", "text": "[IMAGE: Empty URL]"}
            
            # Handle base64 data URLs
            if url.startswith("data:"):
                # Parse data URL: data:image/png;base64,<data>
                # Format: data:[<mediatype>][;base64],<data>
                try:
                    # Split on comma to separate header from data
                    header, base64_data = url.split(",", 1)
                    
                    # Extract media type from header (e.g., "data:image/png;base64")
                    media_type = "image/png"  # Default
                    if header.startswith("data:"):
                        header_content = header[5:]  # Remove "data:"
                        if ";" in header_content:
                            media_type = header_content.split(";")[0]
                        elif header_content:
                            media_type = header_content
                    
                    return {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64_data
                        }
                    }
                except ValueError as e:
                    self._logger.error(f"Failed to parse base64 data URL: {e}")
                    return {"type": "text", "text": "[IMAGE: Invalid data URL]"}
            
            # Handle regular URLs (Anthropic supports URL sources too)
            elif url.startswith("http://") or url.startswith("https://"):
                return {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": url
                    }
                }
            else:
                self._logger.warning(f"Unknown image URL format: {url[:50]}...")
                return {"type": "text", "text": "[IMAGE: Unsupported format]"}
                
        except Exception as e:
            self._logger.error(f"Error converting image to Anthropic format: {e}")
            return {"type": "text", "text": f"[IMAGE: Conversion error]"}

    def _convert_to_client_messages(self, messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        """Convert messages to Anthropic-compatible format.
        
        CRITICAL: This override converts OpenAI image_url format to Anthropic's native format.
        
        Args:
            messages: List of messages
            
        Returns:
            List of messages in Anthropic-compatible format
        """
        # First, use the base class conversion to get standard format
        base_messages = super()._convert_to_client_messages(messages)
        
        # Then convert any image_url items to Anthropic format
        converted_messages = []
        for msg in base_messages:
            content = msg.get("content")
            
            # If content is a list (multimodal), check for image_url items
            if isinstance(content, list):
                converted_content = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "image_url":
                            # Convert OpenAI image_url to Anthropic image format
                            converted_item = self._convert_image_to_anthropic_format(item)
                            converted_content.append(converted_item)
                            self._logger.debug(f"🔄 Converted image_url to Anthropic format")
                        elif item.get("type") == "image":
                            # Already in Anthropic format, keep as-is
                            converted_content.append(item)
                        else:
                            # Keep other items (text, etc.) as-is
                            converted_content.append(item)
                    else:
                        converted_content.append(item)
                
                converted_messages.append({
                    **msg,
                    "content": converted_content
                })
            else:
                # Non-multimodal content, keep as-is
                converted_messages.append(msg)
        
        return converted_messages
        
    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"anthropic-{self._client.model_type}-adapter"


class OpenRouterAdapter(LLMClientAdapter):
    """Adapter for OpenRouter client - OpenAI-compatible format.

    Since OpenRouter uses OpenAI-compatible API, this adapter
    inherits all functionality from the base class.

    OpenRouter provides unified access to Grok, Kimi, Qwen, and 100+ other models.
    """

    def __init__(self, client, **kwargs):
        """Initialize with an OpenRouter client.

        Args:
            client: An initialized OpenRouter client
        """
        # Avoid circular import by importing here
        from modules.llm.openrouter_client import OpenRouterClient
        super().__init__(client, **kwargs)

    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"openrouter-{self._client.model_type}-adapter"


__all__ = [
    # Base classes
    "BaseChatModel",
    "LLMClientAdapter",

    # Provider adapters
    "OpenAIAdapter",
    "AnthropicAdapter",
    "DeepSeekAdapter",
    "GeminiAdapter",
    "OpenRouterAdapter",

    # Specialized adapters
    "DeepSeekAgentAdapter",
    "GeminiAgentAdapter",
]