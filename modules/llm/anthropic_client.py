"""Anthropic Claude API Client."""

from modules.llm.billing_guard import inference_sdk
import json
import logging
import asyncio
from typing import List, Dict, Any, Optional, Union, Tuple

import anthropic
from anthropic.types import Message

from modules.llm.llm_client import LLMClient
from modules.llm.prefix_stamp import stamp_request  # request prefix identity
from modules.llm.anthropic_sampling import clamp_thinking as _clamp_thinking, context_room, route_sampling
from modules.llm.anthropic_wire import (
    apply_thinking_controls, is_tool_schema_error, note_input_transformations, raise_on_refusal,
    thinking_controls_unavailable, wire_messages, wire_metadata)
from modules.llm.call_scope import call_timeout_param, scoped_foundation_len
from modules.llm.deferred_tools import (  # Anthropic native deferred tools
    call_with_deferral_retry, deferred_tools_enabled, prepare_deferred_tools, stream_with_deferral_retry)
from modules.llm.token_counter import count_messages_tokens, count_tools_tokens
from modules.llm.llm_client import raise_llm_error, translate_llm_error
from modules.llm.credit_errors import raise_if_credit_refusal  # D3: 1113 = no balance
from core.exceptions import LLMError, LLMConnectionError, ServiceError
from core.config import BotConfig
import os

# Import default model from registry
from modules.llm.llm_client_registry import get_default_model


def _build_cached_system_param(system, ttl=None):
    """Build Anthropic ``system`` as content blocks with a prompt-cache breakpoint.

    Flow-efficiency D4-a: the system prompt (and the tool definitions that precede
    it in the request) are stable across steps within a session, so marking the
    LAST system block with ``cache_control: ephemeral`` lets Anthropic serve the
    whole tools+system prefix from cache (~10x cheaper on repeated input).

    Args:
        system: the system prompt as a ``str`` or a list of content-block dicts.
        ttl: the resolved cache ttl ("1h") or None for the API default 5-minute
            window, which is emitted as the BARE ephemeral marker.

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

    from modules.llm.cache_hints import prompt_cache_enabled, cache_control_marker
    if prompt_cache_enabled() and blocks:
        blocks[-1] = {**blocks[-1], "cache_control": cache_control_marker(ttl)}
    return blocks


def _apply_conversation_cache(messages, n: int = 3, ttl=None, foundation_len=None):
    """Mark ``n`` conversation messages with ``cache_control: ephemeral``.

    Anthropic allows up to **4** cache breakpoints per request.
    ``_build_cached_system_param`` spends 1 on the system block; this spends the
    remaining 3 inside ``messages``. WHERE they go is policy and lives in the
    cache seam (``modules/llm/cache_hints.py``): with ``foundation_len`` known,
    one marker pins the end of the foundation and two ride the last completed
    tool transactions; without it, the last-``n`` rows.

    Does not mutate the caller's list. No-op (returns input unchanged) when
    ``ANTHROPIC_PROMPT_CACHE`` is disabled or ``messages`` is empty.
    """
    from modules.llm.cache_hints import mark_conversation_cache
    return mark_conversation_cache(messages, n=n, ttl=ttl, foundation_len=foundation_len)


#: The kwargs a caller may set on the wire. `metadata` is filtered further, and the sampling
#: params go through route_sampling.
_WIRE_KWARGS = frozenset({'top_p', 'top_k', 'stop_sequences'})


class AnthropicClient(LLMClient):
    """Anthropic Claude API client.
    
    All model configuration comes from model_registry (single source of truth).
    Deprecated fallback dicts removed Dec 13, 2025.
    """

    _PROVIDER_LABEL = "Anthropic"

    # Only the REAL Anthropic API is known to accept a ``ttl`` inside
    # ``cache_control``. An Anthropic-compat seat (z.ai / kimi / minimax via
    # AnthropicCompatClient) is a third-party /v1/messages validator that may
    # 4xx on a key it does not know, so it overrides this to False and keeps
    # emitting the bare ephemeral marker.
    _SUPPORTS_CACHE_TTL = True

    # Only the REAL Anthropic API knows `defer_loading` / `tool_addition`.
    _SUPPORTS_DEFERRED_TOOLS = True

    # Only the REAL API takes thinking blocks back, `output_config` and `block_binding`.
    _SUPPORTS_THINKING_CONTROLS = True

    # The real API has no sampling params on current models (anthropic_sampling).
    _COMPAT_SEAT = False

    def _cache_ttl(self):
        """The prompt-cache ttl for this request, or None for the bare marker.

        None is BOTH "no ttl stamped" and "5m" — the 5-minute window is the API
        default and is expressed by omitting the key, so an unstamped client
        issues the request without a ttl. Fail-open on any error.
        """
        if not self._SUPPORTS_CACHE_TTL:
            return None
        try:
            from modules.llm.cache_hints import cache_ttl_for
            return cache_ttl_for()
        except Exception:  # pragma: no cover - defensive
            return None

    def __init__(self, config: BotConfig, name: str = "anthropic_client"):
        """Initialize the client."""
        super().__init__(config=config, name=name)
        self._client = None
        
        # Model and API configuration
        self.model_type = config.get_llm_config()['anthropic'].get('model') or get_default_model('anthropic')
        self.api_key = config.get_llm_config()['anthropic']['api_key']
        
        # Get defaults from model_registry (single source of truth)
        from modules.llm.model_registry import get_model_config
        model_config = get_model_config(self.model_type)
        
        self.max_tokens = model_config.max_completion_tokens or 8192

        # Resolve vision support via base helper (falls back to True if not in registry)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = None  # The real API takes no default; see anthropic_sampling

        self.logger.debug(
            f"{self.provider_label} client initialized: model={self.model_type}, "
            f"supports_vision={self.supports_vision}"
        )

    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError(f"{self.provider_label} API key not provided")

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
            self._client = anthropic.AsyncAnthropic(**kwargs, **self._sdk_limits())
            self.logger.debug(f"{self.provider_label} client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up {self.provider_label} client: {e}")
        
    async def _initialize(self) -> None:
        """Initialize the client."""
        if self._initialized:
            return
        try:
            # Validate configuration
            self._validate_llm_config()
            
            await self._setup_client()

            # Validate connection (skipped for fast startup; clients validate
            # lazily on first real call — matches the base client and gemini)
            if not self._skip_validate:
                await self._validate_connection()

            self._initialized = True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize {self.provider_label} client: {e}")
            raise ServiceError(f"Failed to initialize {self.provider_label} client: {e}")

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

        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()

            # Param assembly extracted to _build_tool_api_params so the
            # true-streaming path issues byte-identical requests.
            api_params = self._build_tool_api_params(
                messages, tools, system, temperature, max_tokens, kwargs
            )

            # Use streaming for high max_tokens to avoid Anthropic SDK error
            # "Streaming is required for operations that may take longer than 10 minutes"
            # This happens when max_tokens is high (e.g., 65536). A thinking budget raises
            # max_tokens above 8192, so this path is always taken when thinking is on.
            use_streaming = api_params['max_tokens'] > 8192
            stamp_request(api_params)   # prefix identity

            # On a 400 naming the beta, rebuilds un-deferred and retries once.
            self.last_response = await call_with_deferral_retry(
                self, api_params, use_streaming)


            # Process response - extract both content and tool calls
            response_text, tool_calls = self._parse_agent_blocks(self.last_response)


            # Extract usage data
            usage_data = self._extract_usage_data()

            return response_text, tool_calls, usage_data

        except Exception as e:
            self.logger.error(f"{self.provider_label} tool calling error: {e}")


            # A retry with no tools is a reply with no action. Only a 400 that names the
            # tools array can be cured by leaving them out.
            if not is_tool_schema_error(e, translate_llm_error(e)):
                raise_if_credit_refusal(self.provider_label, e, self.logger)
                raise_llm_error(e, self.provider_label, model=self.model_type)

            self.logger.warning(f"Falling back to non-tool generation due to a tool schema error: {e}")
            response = await self._generate(messages, system, temperature, max_tokens, **kwargs)
            return response, [], self._extract_usage_data()

    def _estimate_input_tokens(self, messages: List[Dict[str, Any]], system: Optional[Any]) -> int:
        """The rows and the system prompt, with a recovered system prompt counted once."""
        rows = [m for m in messages if m.get('role') != 'system']
        if system:
            rows = [{"role": "system", "content": system}, *rows]
        return count_messages_tokens(rows, self.model_type)

    def _thinking_controls_on(self) -> bool:
        return self._SUPPORTS_THINKING_CONTROLS and not thinking_controls_unavailable(self)

    def _request_params(self, rows: List[Dict[str, Any]], system: Optional[Any],
                        temperature: Optional[float], max_tokens_value: int,
                        kwargs: Dict[str, Any],
                        tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """The one assembly of a request: late tools first, then the cache markers on the
        rows that go out, the thinking controls, the sampling route and the call's timeout."""
        api_params = {k: v for k, v in kwargs.items() if k in _WIRE_KWARGS}
        api_params.update(model=self.model_type, messages=rows, max_tokens=max_tokens_value)
        metadata = wire_metadata(kwargs.get('metadata'))
        if metadata:
            api_params['metadata'] = metadata
        temp = temperature if temperature is not None else self.temperature
        if temp is not None:
            api_params['temperature'] = temp
        if tools is not None:
            api_params['tools'] = tools
            api_params['tool_choice'] = kwargs.get('tool_choice', {'type': 'auto'})
        system_param = _build_cached_system_param(system, ttl=self._cache_ttl())
        if system_param is not None:
            api_params['system'] = system_param
        api_params = prepare_deferred_tools(
            api_params, enabled=tools is not None and deferred_tools_enabled(self))
        # 4 breakpoints — system, the end of the foundation, 2 transactions.
        api_params['messages'] = _apply_conversation_cache(
            api_params['messages'], ttl=self._cache_ttl(),
            foundation_len=scoped_foundation_len())
        if self._thinking_controls_on():
            from modules.llm.model_registry import get_model_config
            apply_thinking_controls(api_params, getattr(get_model_config(self.model_type), "capabilities", None))
        api_params.update(call_timeout_param())
        return route_sampling(api_params, compat_seat=self._COMPAT_SEAT)

    def _build_tool_api_params(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        system: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """The messages.create/stream params of the tool path, for the batch call and for
        astream_agent_response, so both issue byte-identical requests."""
        rows, system = wire_messages(messages, system, tool_blocks=True,
                                     keep_thinking=self._thinking_controls_on())

        # Clamp max_tokens to model limits (single canonical logic on base)
        max_tokens_value = self._adjust_max_tokens(
            messages=messages,
            max_tokens=max_tokens,
            estimated_input_tokens=self._estimate_input_tokens(messages, system),
            tools=tools,
        )

        self.logger.info(f"{self.provider_label} API request with tools: model={self.model_type}, tools={len(tools)}, max_tokens={max_tokens_value}")

        api_params = self._request_params(
            rows, system, temperature, max_tokens_value, kwargs, tools=tools)

        # Extended thinking on a row older than 4.6, the one kind with a budget (gated,
        # default OFF). A later row takes `output_config.effort` from its capability.
        from modules.llm.model_registry import (
            thinking_config_enabled, get_thinking_config, get_model_config,
        )
        if self._thinking_controls_on() and 'thinking' not in api_params and thinking_config_enabled():
            tcfg = get_thinking_config(self.model_type)
            budget = tcfg.get('budget_tokens')
            if budget:
                # Clamp so max_tokens > budget AND max_tokens <= the model cap
                # (registry entries set budget == cap, which overran the cap -> 400).
                _cfg = get_model_config(self.model_type)
                model_cap = getattr(_cfg, 'max_completion_tokens', None) if _cfg else None
                budget, max_tokens_value = _clamp_thinking(model_cap, budget, max_tokens_value, room=context_room(self, messages, system, count_tools_tokens(tools)))
                if budget:
                    api_params['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
                    api_params['max_tokens'] = max_tokens_value
        return api_params

    def _parse_agent_blocks(self, response) -> Tuple[str, List[Dict[str, Any]]]:
        """Extract (text, tool_calls) from an Anthropic message's content blocks.

        Shared by every path, so a refusal raises typed on all of them.
        """
        raise_on_refusal(response, self.provider_label, self.model_type)
        note_input_transformations(response)
        response_text = ""
        tool_calls = []
        # Process response - extract both content and tool calls
        if hasattr(response, 'content') and response.content:
            for block in response.content:
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

        return response_text.strip(), tool_calls

    async def astream_agent_response(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs
    ):
        """TRUE token streaming: yield text deltas, then one final event.

        Async generator yielding ``{"type": "text", "text": str}`` per
        content_block text delta, then exactly one ``{"type": "final",
        "content", "tool_calls", "usage_data", "response_id"}`` built with the
        SAME parsing/usage extraction as generate_agent_response. The request
        params are byte-identical to the batch path (_build_tool_api_params).
        Only consumed when LLM_TOKEN_STREAMING is on (adapters.astream).
        """
        if not self._initialized:
            await self._initialize()

        system = kwargs.pop('system', None)
        temperature = kwargs.pop('temperature', None)
        max_tokens = kwargs.pop('max_tokens', None)

        api_params = self._build_tool_api_params(
            messages, tools or [], system, temperature, max_tokens, kwargs
        )
        if not tools:
            # An empty tools list with tool_choice is invalid — drop both.
            api_params.pop('tools', None)
            api_params.pop('tool_choice', None)

        # Stamps, streams, self-heals ONCE before the first token is yielded.
        async for kind, item in stream_with_deferral_retry(self, api_params):
            if kind == "text":
                yield {"type": "text", "text": item}
            else:
                final = item

        self.last_response = final
        response_text, tool_calls = self._parse_agent_blocks(final)
        usage_data = self._extract_usage_data()
        yield {
            "type": "final",
            "content": response_text,
            "tool_calls": tool_calls,
            "usage_data": usage_data,
            "response_id": getattr(final, 'id', None),
        }

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
            raise_if_credit_refusal(self.provider_label, e, self.logger)
            self.logger.error(f"generate_agent_response failed: {str(e)}", exc_info=True)
            raise_llm_error(e, self.provider_label, model=self.model_type)

    async def _generate(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Generate a response from Claude API."""
        
        try:
            # Ensure client is initialized
            if not self._initialized:
                await self._initialize()
                
            rows, system = wire_messages(messages, system, tool_blocks=False)

            # Clamp max_tokens to model limits (single canonical logic on base)
            max_tokens_value = self._adjust_max_tokens(
                messages=messages,
                max_tokens=max_tokens,
                estimated_input_tokens=self._estimate_input_tokens(messages, system),
            )

            self.logger.info(f"{self.provider_label} API request: model={self.model_type}, max_tokens={max_tokens_value}")

            api_params = self._request_params(rows, system, temperature, max_tokens_value, kwargs)

            # Use streaming for high max_tokens to avoid Anthropic SDK error
            # "Streaming is required for operations that may take longer than 10 minutes"
            use_streaming = max_tokens_value > 8192
            stamp_request(api_params)   # prefix identity

            if use_streaming:
                self.logger.debug(f"Using streaming for _generate (max_tokens={max_tokens_value})")
                async with inference_sdk(self).messages.stream(**api_params) as stream:
                    self.last_response = await stream.get_final_message()
            else:
                self.last_response = await inference_sdk(self).messages.create(**api_params)
            
            response_text, _ = self._parse_agent_blocks(self.last_response)


            return response_text
        except anthropic.APIError as e:
            self.logger.error(f"{self.provider_label} API error: {e}")
            raise_if_credit_refusal(self.provider_label, e)
            raise_llm_error(e, self.provider_label, model=self.model_type)
        except Exception as e:
            self.logger.error(f"Error generating response from {self.provider_label}: {e}")
            raise_llm_error(e, self.provider_label, model=self.model_type)

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

            filtered_kwargs = kwargs.copy()
            if metadata:
                filtered_kwargs['metadata'] = metadata

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
            raise_llm_error(e, self.provider_label, model=self.model_type)

    async def _validate_connection(self) -> None:
        """Validate connection to Anthropic."""
        try:
            # Simple test completion
            response = await inference_sdk(self).messages.create(
                model=self.model_type,
                max_tokens=10,
                messages=[{"role": "user", "content": "Test connection"}]
            )
            if not response:
                raise LLMConnectionError(f"No response from {self.provider_label} API")
            self.logger.debug(f"{self.provider_label} API connection validated successfully")
        except Exception as e:
            raise_if_credit_refusal(self.provider_label, e, self.logger)
            self.logger.error(f"Failed to validate {self.provider_label} connection: {e}")
            raise ServiceError(f"Failed to validate {self.provider_label} connection: {e}")

    async def _cleanup_client(self) -> None:
        """Clean up Anthropic client."""
        try:
            client, self._client = getattr(self, '_client', None), None
            if client is not None and hasattr(client, 'close'):
                await client.close()  # close the httpx pool, not just drop it
        except Exception as e:
            self.logger.error(f"Error cleaning up {self.provider_label} client: {e}")

    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup_client()
        self._client = None
        self._initialized = False
        self.logger.info(f"{self.provider_label} client cleaned up")

    async def _make_validation_request(self) -> Any:
        """Make minimal test request to Anthropic."""
        return await inference_sdk(self).messages.create(
            model=self.model_type,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )

    def _check_validation_response(self, response: Any) -> None:
        """Validate Anthropic response."""
        if not response or not response.content:
            raise ValueError(f"Invalid response from {self.provider_label}")

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

        # An Anthropic-compat endpoint
        # that returns output usage but NO input usage leaves the meter (and
        # any cache-hit verification) input-blind while telemetry silently
        # records prompt_tokens=0. Warn ONCE per client instance, naming the
        # provider and the raw payload, so the gap is diagnosable.
        if output_tokens and not input_tokens:
            if not getattr(self, '_input_usage_warned', False):
                self._input_usage_warned = True
                try:
                    raw = usage.model_dump() if hasattr(usage, 'model_dump') else vars(usage)
                except Exception:
                    raw = repr(usage)
                self.logger.warning(
                    f"{self.provider_label}: input-side usage missing from response "
                    f"(output_tokens={output_tokens}, raw usage={raw}) — prompt-token "
                    f"metering and cache-hit metrics are blind on this endpoint"
                )

        # Anthropic reports cache reads/writes SEPARATELY and EXCLUDES them from
        # input_tokens (total input = input_tokens + cache_read + cache_creation).
        # Fold them back so the meter sees full input, expose cache READS as
        # cached_tokens (billed at 0.1x) and cache CREATION as cache_creation_tokens
        # (billed at 1.25x via ModelPricing.cache_write_price downstream).
        cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
        cache_creation = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        total_input = None if input_tokens is None else input_tokens + cache_read + cache_creation
        from modules.llm.usage_extract import cache_creation_1h_from  # 1h write slice (2.0x)

        # Calculate total
        total_tokens = None
        if total_input is not None and output_tokens is not None:
            total_tokens = total_input + output_tokens

        usage_data = {
            'prompt_tokens': total_input,
            'completion_tokens': output_tokens,
            'total_tokens': total_tokens,
            'cached_tokens': cache_read,
            'cache_creation_tokens': cache_creation,
            'cache_creation_1h_tokens': cache_creation_1h_from(usage, cache_creation),
        }

        self.logger.debug(f"Extracted usage: {usage_data}")
        return usage_data