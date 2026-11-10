"""LLM adapters for custom LLM clients.

This module provides adapters to convert our custom LLM clients into
a consistent interface for use with the agents system.

- Vision support detection uses model_registry as the single source of truth.
- Image format conversion is provider-specific (Anthropic requires a different format).
- Uses native message types from modules.llm.messages.
"""

import logging
import os
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

# Native message types
from modules.llm.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
    ChatGeneration, ChatResult
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# The four provider-client imports are TYPE_CHECKING-only.
# An eager import would pull the anthropic/openai/google.generativeai SDKs into
# EVERY entry-point import (cli.polyrob, api.app) via agents.task.agent -> service ->
# adapters — the client classes are used here only as __init__ annotations; the real
# instances are constructed by llm_factory, which imports the client modules lazily.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from modules.llm.anthropic_client import AnthropicClient
    from modules.llm.gemini_client import GeminiClient
    from modules.llm.openai_client import OpenAIClient
from modules.llm.llm_client import LLMClient, translate_llm_error
from modules.llm.model_registry import get_model_config
from modules.llm.retry_log import log_attempt_classified
from modules.llm.init_gate import note_call_error
from core.exceptions import LLMError, LLMInvalidRequestError, LLMPermanentError, LLMRefusalError
from modules.llm.call_scope import begin_call
from modules.llm.call_result import NATIVE_DEEPSEEK_SEAT, REASONING_SEAT_KEY, call_facts


class _StreamEndedEarly(LLMError):
    """The provider's stream closed with no final event."""


def _is_stream_specific(error: BaseException) -> bool:
    """Only the stream itself failed, so a batch call can still answer: it closed early,
    or the provider refused the request FOR streaming. A 429 or a 5xx is not this: a second
    call at once would pay for the same failure."""
    if isinstance(error, _StreamEndedEarly):
        return True
    return (isinstance(error, (LLMInvalidRequestError, LLMPermanentError))
            and not isinstance(error, LLMRefusalError)
            and "stream" in str(error).lower())


def think_scrubber_enabled() -> bool:
    """Gate for the reasoning-block scrubber. Default ON, fail-open.

    Disable with THINK_SCRUBBER_ENABLED in {0, false, no, off}. Mirrors
    cache_hints.prompt_cache_enabled() semantics.
    """
    return os.getenv("THINK_SCRUBBER_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def token_streaming_enabled() -> bool:
    """Gate for TRUE per-token streaming.

    When ON and the wrapped client implements ``astream_agent_response``,
    ``LLMClientAdapter.astream`` yields real deltas instead of one
    full-response chunk.

    Default ON under the local interactive profile
    (``POLYROB_LOCAL``) — a 60-second turn showing only a spinner was the
    default experience while the whole streaming pipeline sat unreachable.
    Server default stays OFF; an explicit env value wins.
    """
    raw = os.getenv("LLM_TOKEN_STREAMING")
    if raw is not None and raw.strip() != "":
        from core.env import parse_bool
        return parse_bool(raw, False)
    try:
        from core.config_policy.policy import local_mode_enabled
        return bool(local_mode_enabled())
    except Exception:
        return False


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

    @property
    @abstractmethod
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        pass

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


class LLMClientAdapter(BaseChatModel):
    """Base adapter class for converting our LLM clients to a consistent interface."""

    # Use Pydantic's PrivateAttr for client to avoid validation issues
    _client: LLMClient = PrivateAttr(default=None)
    _logger: logging.Logger = PrivateAttr(default=None)
    # Cached vision-support flag (lazily filled by _get_vision_support). MUST be a
    # declared PrivateAttr — Pydantic v2 raises AttributeError on an undeclared
    # private attr, which crashes the first multimodal request.
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
        # Only use client.model_type as fallback if model_name not already provided
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
        """Strip leaked reasoning blocks from assistant text.

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
        
        Uses model_registry.get_model_config() as the canonical source.
        
        Falls back to client.supports_vision attribute if registry lookup fails.
        
        Returns:
            bool: True if model supports vision/multimodal content
        """
        # Return cached value if available
        if self._supports_vision_cached is not None:
            return self._supports_vision_cached
        
        model_type = getattr(self._client, 'model_type', None)
        if model_type:
            self._supports_vision_cached = get_model_config(model_type).capabilities.supports_vision
            self._logger.debug(f"Vision support from registry: {model_type} -> {self._supports_vision_cached}")
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
                    self._replay_reasoning(msg, assistant_msg)
                    client_messages.append(assistant_msg)
                    continue
            else:
                role = "assistant"

            # Handle text vs. multimodal content for non-tool messages
            if isinstance(msg.content, str):
                client_messages.append({"role": role, "content": msg.content})
                if isinstance(msg, AIMessage):
                    self._replay_reasoning(msg, client_messages[-1])
            elif isinstance(msg.content, list):
                # Preserve multimodal content for vision-capable models
                # Use model_registry as single source of truth
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
                if isinstance(msg, AIMessage):
                    self._replay_reasoning(msg, client_messages[-1])

        return client_messages
    
    def _stamp_reasoning(self, ai_message, raw_response) -> None:
        """Keep what the seat's API needs back on the call's OWN message. Default: nothing.
        A seat that needs it overrides this (``AnthropicAdapter``, ``DeepSeekAdapter``)."""

    def _replay_reasoning(self, msg, client_msg: Dict[str, Any]) -> None:
        """Put the stored reasoning on an outgoing assistant message. Default: nothing.
        Prod 2026-11-07: an OpenRouter replay fed a loop of one repeated word up to the cap."""

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

        # A choice needs a tools[] on the wire, and an empty one is a 400 on OpenAI.
        if kwargs.get("tool_choice") is not None and kwargs.get("tools"):
            generation_params["tool_choice"] = self._wire_tool_choice(kwargs["tool_choice"])

        if metadata:
            generation_params["metadata"] = metadata
        from modules.llm.billing_guard import billed_generation_params
        return billed_generation_params(self, generation_params)

    @staticmethod
    def _wire_tool_choice(choice: Any) -> Any:
        """``auto``/``required``/``none`` in this provider's wire shape."""
        return choice

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
        # Cache-WRITE (creation) tokens, billed at a surcharge downstream
        if usage_data.get('cache_creation_tokens'):
            usage_metadata['cache_creation_input_tokens'] = usage_data.get('cache_creation_tokens')
        # The 1h-window slice of that write (Anthropic 2.0x)
        if usage_data.get('cache_creation_1h_tokens'):
            usage_metadata['cache_creation_1h_tokens'] = usage_data.get('cache_creation_1h_tokens')
        # What the provider actually billed. None means "not reported" (keep
        # the estimate); 0.0 is a real free call, so test against None, not falsy.
        for key in ('billed_cost_usd', 'billed_source', 'cache_discount_usd'):
            if usage_data.get(key) is not None:
                usage_metadata[key] = usage_data[key]
        return usage_metadata

    def _plain_call_usage(self) -> Optional[dict]:
        """No-tools path usage from the client's last response; None if unknown."""
        extract = getattr(self._client, '_extract_usage_data', None)
        if not callable(extract):
            return None
        try:
            usage = self._usage_metadata_from(extract())
        except Exception:
            return None
        if not usage or not (usage.get('input_tokens') or usage.get('output_tokens')):
            return None
        return usage

    def _resolve_tools(self, kwargs: dict) -> Any:
        """The call's tools. An EXPLICIT ``tools`` key is authoritative: ``tools=None`` /
        ``tools=[]`` means "no tools" and must NOT inherit the last step's set. The stored
        set serves only a retry path that re-calls with no key; the next tool-less aux call
        (compaction, reflection, the judge) must never go out with the step's toolset."""
        if 'tools' in kwargs:
            return kwargs['tools']
        tools = getattr(self, '_pending_tools', None)
        if tools:
            self._pending_tools = None
        return tools

    async def _reserve_call(self) -> Optional[str]:
        from core.billing_context import reserve_billed_call
        from modules.llm.aux_metering import _llm_identity
        return await reserve_billed_call(*_llm_identity(self))

    def _finish_call(self, content: str, tool_calls: Optional[list], usage_metadata: Optional[dict],
                     raw_response: Any, response_id: Optional[str], request: Any,
                     reservation_id: Optional[str], kwargs: dict) -> AIMessage:
        """The ONE post-call path of batch and stream: the call's own message and facts.

        Runs in the tick the client returned or yielded its final event, with no await
        since. Every consumer reads this message, because the client serves other tasks.
        """
        message = AIMessage(content=content, usage_metadata=usage_metadata)
        if tool_calls:
            message.tool_calls = tool_calls
        self._stamp_reasoning(message, raw_response)
        message.call_facts = call_facts(raw_response, usage_metadata, request)
        if response_id is None and raw_response is not None:
            response_id = (raw_response.get('id') if isinstance(raw_response, dict)
                           else getattr(raw_response, 'id', None))
        if isinstance(response_id, str) and response_id:
            message._polyrob_provider_response_id = response_id
        if reservation_id:
            message._polyrob_billing_reservation_id = reservation_id
        # The call SUCCEEDED, so no retry path needs the stored set. A failure raises
        # before this line and leaves it armed for the retry.
        if kwargs.get('tools') and getattr(self, '_pending_tools', None) is kwargs['tools']:
            self._pending_tools = None
        return message

    def _raise_translated(self, error: Exception) -> None:
        """A typed client error passes unchanged, so the agent can run its recovery."""
        label = getattr(self._client, 'provider_label', type(self._client).__name__)
        translated = translate_llm_error(error, f"from {label}",
                                         model=getattr(self._client, 'model_type', None))
        # Per-attempt WARNING; the outer retry loop owns the ERROR.
        log_attempt_classified(self._logger, error, translated)
        note_call_error(self._client, translated)  # health from real calls
        if translated is error:
            raise error
        raise translated from error

    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, *,
                         reservation_id: Optional[str] = None, **kwargs) -> ChatResult:
        """One batch call. *reservation_id* is the hold of a stream that this call replaces."""
        from modules.llm.billing_guard import check_adapter, fresh_credential
        await fresh_credential(self._client)
        check_adapter(self)
        client_messages = self._convert_to_client_messages(messages)
        generation_params = self._build_generation_params(stop, kwargs)
        tools = self._resolve_tools(kwargs)
        reservation_id = reservation_id or await self._reserve_call()
        request = begin_call()
        # An exception/cancellation retains the reservation: an absent response is not
        # evidence that the provider did no work.
        try:
            if tools and hasattr(self._client, 'generate_agent_response'):
                response = await self._client.generate_agent_response(
                    messages=client_messages, tools=tools, **generation_params)
                content, tool_calls, usage_data = self._unpack_tool_gen_result(response)
                usage_metadata = self._usage_metadata_from(usage_data)
            else:
                if tools:
                    self._logger.warning(f"Tools provided but client {self._client.__class__.__name__} "
                                         "lacks generate_agent_response")
                content = await self._client.generate_response(
                    messages=client_messages, **generation_params)
                tool_calls = None
                usage_metadata = self._plain_call_usage()
            raw_response = getattr(self._client, 'last_response', None)
            message = self._finish_call(self._scrub_content(content or ""), tool_calls, usage_metadata,
                                        raw_response, None, request, reservation_id, kwargs)
            generation = ChatGeneration(message=message)
            if raw_response:
                generation.generation_info = {"raw": raw_response}
            return ChatResult(generations=[generation])
        except Exception as e:
            self._raise_translated(e)

    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return "llm-client-adapter"

    async def ainvoke(self, input, config=None, *, stop=None, **kwargs) -> AIMessage:
        """Pass the tools through kwargs to _agenerate, and keep them for a retry path that
        re-calls with no key."""
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        if 'tools' in kwargs and kwargs['tools']:
            self._pending_tools = kwargs['tools']

        result = await self._agenerate(messages, stop=stop, **kwargs)
        if result.generations:
            return result.generations[0].message
        return AIMessage(content="")

    async def astream(self, input, config=None, *, stop=None, **kwargs):
        """Stream a response, passing tools through kwargs.

        Default: the full ainvoke() result as one chunk. With `LLM_TOKEN_STREAMING`:
        when the flag is ON and the client implements ``astream_agent_response``, TRUE
        per-token deltas via ``_astream_true``. A stream-specific failure BEFORE the first
        chunk falls back to the single chunk on the same billing reservation. Any other
        failure propagates: the agent loop's retry machinery owns it, and a second call
        at once would pay twice (after a chunk it would also double the text).
        """
        if 'tools' in kwargs and kwargs['tools']:
            self._pending_tools = kwargs['tools']

        reservation_id = None
        if token_streaming_enabled() and hasattr(self._client, 'astream_agent_response'):
            from modules.llm.billing_guard import check_adapter, fresh_credential
            await fresh_credential(self._client)
            check_adapter(self)
            reservation_id = await self._reserve_call()
            yielded_any = False
            try:
                async for chunk in self._astream_true(input, stop=stop, reservation_id=reservation_id,
                                                      **kwargs):
                    yielded_any = True
                    yield chunk
                return
            except Exception as e:
                if yielded_any or not _is_stream_specific(e):
                    raise
                self._logger.warning(
                    f"true token streaming failed before first chunk; "
                    f"falling back to single-chunk: {type(e).__name__}: {e}"
                )

        response = await self.ainvoke(input, config=config, stop=stop,
                                      reservation_id=reservation_id, **kwargs)
        yield response

    async def _astream_true(self, input, *, stop=None, reservation_id=None, **kwargs):
        """TRUE token streaming: scrubbed text deltas, then ONE final
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
        carries the full content, single-chunk behavior. (2) Once
        live, a rolling window watches for the brain-state marker
        (``"current_state"`` — e.g. prose followed by a TRAILING brain block);
        on sight the stream MUTES: no further live deltas, the remainder rides
        the final chunk as one piece, where the downstream whole-chunk brain
        scrub works. (3) hitl_manager's per-chunk scrub stays as backstop.
        """
        from modules.llm.billing_guard import check_adapter, fresh_credential
        await fresh_credential(self._client)
        check_adapter(self)
        # Convert input to messages (mirrors ainvoke)
        if isinstance(input, list):
            messages = input
        elif hasattr(input, 'to_messages'):
            messages = input.to_messages()
        else:
            messages = [HumanMessage(content=str(input))]

        client_messages = self._convert_to_client_messages(messages)
        generation_params = self._build_generation_params(stop, kwargs)
        tools = self._resolve_tools(kwargs)
        reservation_id = reservation_id or await self._reserve_call()

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
        final_raw = None
        _BRAIN_MARKER = '"current_state"'

        request = begin_call()
        events = self._client.astream_agent_response(
            messages=client_messages, tools=tools, **generation_params)
        while True:
            try:
                event = await events.__anext__()
            except StopAsyncIteration:
                break
            except Exception as e:
                self._raise_translated(e)
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
                final_raw = getattr(self._client, 'last_response', None)

        if final_event is None:
            raise _StreamEndedEarly(
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
            # chunk carries the WHOLE scrubbed completion.
            final_content = self._scrub_content(final_event.get("content") or "")

        yield self._finish_call(final_content, final_event.get("tool_calls"),
                                self._usage_metadata_from(final_event.get("usage_data")),
                                final_raw, final_event.get("response_id"), request,
                                reservation_id, kwargs)


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
        
    @staticmethod
    def _wire_tool_choice(choice: Any) -> Any:
        return {"auto": {"type": "auto"}, "required": {"type": "any"},
                "none": {"type": "none"}}.get(choice, choice)

    def _convert_image_to_anthropic_format(self, image_item: Dict[str, Any]) -> Dict[str, Any]:
        """Convert OpenAI image_url format to Anthropic image format."""
        from modules.llm.anthropic_images import convert_image_to_anthropic_format
        return convert_image_to_anthropic_format(image_item, self._logger)

    def _stamp_reasoning(self, ai_message, raw_response) -> None:
        """Keep the call's thinking blocks on its OWN message (anthropic_wire)."""
        if getattr(self._client, '_SUPPORTS_THINKING_CONTROLS', False):
            from modules.llm.anthropic_wire import thinking_record
            record = thinking_record(raw_response)
            if record:
                ai_message.additional_kwargs['anthropic_thinking'] = record

    def _replay_reasoning(self, msg, client_msg: Dict[str, Any]) -> None:
        blocks = (getattr(msg, 'additional_kwargs', None) or {}).get('anthropic_thinking')
        if blocks:
            client_msg['thinking_blocks'] = blocks

    def _convert_to_client_messages(self, messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        """The base conversion, with OpenAI ``image_url`` items in Anthropic's image format."""
        converted = []
        for msg in super()._convert_to_client_messages(messages):
            content = msg.get("content")
            if isinstance(content, list):
                msg = {**msg, "content": [
                    self._convert_image_to_anthropic_format(item)
                    if isinstance(item, dict) and item.get("type") == "image_url" else item
                    for item in content]}
            converted.append(msg)
        return converted

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
        super().__init__(client, **kwargs)

    @property
    def _llm_type(self) -> str:
        """Return the type of LLM."""
        return f"openrouter-{self._client.model_type}-adapter"


# The native DeepSeek seat is the ONE seat that sends stored reasoning back: its API, in
# thinking mode with tools, wants each earlier assistant message's ``reasoning_content``.
# Only text this seat wrote (tag ``call_result.NATIVE_DEEPSEEK_SEAT``) goes back; other text
# goes back as "" (prod 2026-11-07: an OpenRouter replay looped, llm-integration.md). It lives
# in this module, not its own, so it adds no module to the import cycle (layering ratchet).
class DeepSeekAdapter(OpenRouterAdapter):
    """The OpenAI-compatible adapter plus DeepSeek's reasoning round trip."""

    def _stamp_reasoning(self, ai_message, raw_response) -> None:
        try:
            message = raw_response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            return
        reasoning = getattr(message, 'reasoning_content', None)
        if isinstance(reasoning, str) and reasoning:
            ai_message.additional_kwargs['reasoning_content'] = reasoning
            ai_message.additional_kwargs[REASONING_SEAT_KEY] = NATIVE_DEEPSEEK_SEAT

    def _replay_reasoning(self, msg, client_msg: Dict[str, Any]) -> None:
        stored = getattr(msg, 'additional_kwargs', None) or {}
        reasoning = stored.get('reasoning_content')
        own = stored.get(REASONING_SEAT_KEY) == NATIVE_DEEPSEEK_SEAT
        client_msg['reasoning_content'] = reasoning if own and isinstance(reasoning, str) else ""

    @property
    def _llm_type(self) -> str:
        return f"deepseek-{self._client.model_type}-adapter"


__all__ = [
    # Base classes
    "BaseChatModel",
    "LLMClientAdapter",

    # Provider adapters
    "OpenAIAdapter",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OpenRouterAdapter",
    "DeepSeekAdapter",
]
