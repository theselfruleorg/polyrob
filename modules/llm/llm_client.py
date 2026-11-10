"""Base LLM client implementation."""

from typing import Optional, List, Dict, AsyncGenerator, Any, NoReturn, Union, Tuple
from abc import abstractmethod

from core.credit_sentinel import looks_like_credit_death, looks_like_transient_budget
from core.llm_limits import llm_request_timeout
from core.exceptions import (
    InsufficientCreditsError,
    LLMError,
    LLMResponseError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMPermanentError,
)

# Runtime imports
from modules.base_module import BaseModule
from core.config import BotConfig

# Import model registry and token counter for intelligent configuration
from modules.llm.model_registry import get_model_config
from modules.llm.token_counter import count_tokens, count_messages_tokens, count_tools_tokens
from modules.llm.profiles import get_profile
from modules.llm.billing_guard import fresh_credential
from core.stop_reason import normalize_stop
from modules.llm.call_scope import anchored_input_tokens
from modules.llm.error_rules import (
    ACCOUNT_RE, DIGITS_RE, NOT_FOUND_RE, ProviderBodyError, class_for_status, class_for_text, filtered_error,
)


def _status_code(error: BaseException) -> Optional[int]:
    """The HTTP status on the exception or on what it wraps: ``status_code`` (openai,
    anthropic), ``response.status_code`` (httpx), an integer ``code`` (google, a body
    error). openai's own ``code`` is a string and is not read."""
    current: Optional[BaseException] = error
    for _ in range(8):
        if current is None:
            return None
        for value in (getattr(current, "status_code", None),
                      getattr(getattr(current, "response", None), "status_code", None),
                      getattr(current, "code", None)):
            if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
                return int(value)
        current = current.__cause__ or current.__context__
    return None


def translate_llm_error(error: BaseException, context: str = "",
                        *, model: Optional[str] = None) -> BaseException:
    """The one classifier of a provider error. Callers raise what this returns.

    An already typed error passes unchanged, and so does a credit-ledger refusal.
    Otherwise the HTTP status decides, with the body read only where one status has two
    meanings (a 400 can be an overflow, a 429 can be a dead account). With no status the
    word-bounded text decides, context length first, because an overflow message holds
    numbers (``modules/llm/error_rules.py``). ``model`` is named in a not-found error,
    which is permanent: a retry cannot revive a retired model. A route with no upstream
    now is not that error: another route may serve the model.
    """
    if isinstance(error, (LLMError, InsufficientCreditsError)):
        return error

    text = str(error) or type(error).__name__
    low = text.lower()
    # Digits out: the credit marker also reads a bare 402, and a count is not a status.
    facts = {
        "no_credit": looks_like_credit_death(DIGITS_RE.sub(" ", low)) or bool(ACCOUNT_RE.search(low)),
        "transient_budget": looks_like_transient_budget(low),
    }
    status = _status_code(error)
    if status is not None:
        cls = class_for_status(status, low, **facts)
    elif isinstance(error, (TimeoutError, ConnectionError)):
        cls = LLMConnectionError
    else:
        cls = class_for_text(low, **facts)

    if cls is LLMPermanentError and (status == 404 or (status is None and NOT_FOUND_RE.search(low))):
        text = f"model {model or '(unknown)'} not found (404), and a retry cannot help: {text}"
    return cls(f"{context}: {text}" if context else text)


def raise_llm_error(error: BaseException, context: str = "",
                    *, model: Optional[str] = None) -> NoReturn:
    """Raise the typed form of ``error``, chained to it. An already typed error is
    raised as it is."""
    translated = translate_llm_error(error, context, model=model)
    if translated is error:
        raise error
    raise translated from error


_DEFAULT_OUTPUT_TOKEN_CAP = 16384

#: Below this much room, a call cannot produce a usable answer and is not sent.
_MIN_USEFUL_OUTPUT_TOKENS = 256


def _output_token_cap() -> int:
    """Absolute per-request completion-token ceiling (env ``LLM_MAX_OUTPUT_TOKENS``),
    narrowed by this session's ``AUTONOMOUS_MAX_OUTPUT_TOKENS`` budget when one
    was stamped (latency is output-bound; see modules/llm/output_budget).

    Models advertise huge completion limits (e.g. GLM-5.2 = 262144). Requesting
    the full ceiling wastes cost and makes credit-metered providers (OpenRouter)
    pre-authorize the entire amount, producing spurious HTTP 402s when the balance
    can't cover it. Cap every request to a sane default; override per-deployment.
    """
    from modules.llm.output_budget import output_token_cap
    return output_token_cap(_DEFAULT_OUTPUT_TOKEN_CAP)


class LLMClient(BaseModule):
    """Base class for LLM clients."""

    # Resolved prompt-cache strategy, stamped by create_chat_model. "none" until
    # stamped so any client that bypasses the factory gets no caching.
    cache_strategy: str = "none"
    model_type: Optional[str] = None
    _PROVIDER_LABEL: Optional[str] = None

    DEFAULT_MAX_RETRIES = 3

    def __init__(self, config: BotConfig, name: str = "llm_client", container=None):
        """Initialize LLM client."""
        super().__init__(name=name, config=config, container=container)
        self.model_type = None  # Will be set by implementations
        self.last_response = None
        self._retries = 0
        self._max_retries = config.get('max_retries', 3)
        self._retry_delay = config.get('retry_delay', 1)
        self.api_key = None
        
        # Initialize with intelligent defaults that will be updated by model registry
        self.temperature = 0.7
        self.max_tokens = 8000  # Conservative default, will be updated by registry
        
        # Validate config if needed
        self._validate_config()

    def _sdk_limits(self) -> Dict[str, Any]:
        """``timeout`` and ``max_retries`` for every SDK client constructor. Without them
        the SDK waits 600 s per attempt. ``LLM_REQUEST_TIMEOUT_SECONDS`` moves the timeout."""
        return {
            "timeout": float(llm_request_timeout()),
            "max_retries": self.DEFAULT_MAX_RETRIES,
        }

    @property
    def provider_label(self) -> str:
        """The provider's name in every log line and error: the spec's display name on a
        spec-driven client, else the client's own label, else its class name."""
        spec = getattr(self, "_spec", None)
        return spec.display_name if spec else (
            self._PROVIDER_LABEL or type(self).__name__.replace("Client", ""))

    def _first_choice(self, response: Any) -> Any:
        """The usable choice of a 200 response. An error in the body and a filtered turn
        raise a typed error here, so neither reaches the agent as an empty action."""
        label = self.provider_label
        body_error = getattr(response, "error", None)
        if body_error and isinstance(body_error, (dict, str)):
            raise translate_llm_error(ProviderBodyError(body_error), label, model=self.model_type)
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMResponseError(f"{label}: no choices in the response for model {self.model_type}")
        choice = choices[0]
        choice_error = getattr(choice, "error", None)
        if choice_error and isinstance(choice_error, (dict, str)):
            raise translate_llm_error(ProviderBodyError(choice_error), label, model=self.model_type)
        stop = normalize_stop(getattr(choice, "finish_reason", None))
        if stop == "error":
            raise LLMResponseError(f"{label}: finish_reason=error for model {self.model_type}")
        if stop == "filtered":
            raise filtered_error(label, self.model_type, getattr(choice, "finish_reason", None))
        return choice

    def _configure_from_model_registry(self) -> None:
        """Configure client parameters from model registry."""
        model_config = get_model_config(self.model_type)
        self.max_tokens = model_config.max_completion_tokens
        self.logger.debug(f"Model {self.model_type} configured: context={model_config.context_window}, "
                          f"completion={model_config.max_completion_tokens}")

    # ------------------------------------------------------------------
    # Task 2.4: shared helpers de-duplicated from per-provider clients
    # ------------------------------------------------------------------

    def _resolve_supports_vision(self, model_type: Optional[str] = None) -> bool:
        """The registry's answer for *model_type* (default ``self.model_type``)."""
        name = model_type if model_type is not None else self.model_type
        return get_model_config(name).capabilities.supports_vision

    def _resolve_profile_base_url(self, provider: str) -> Optional[str]:
        """Return the base_url from the provider's ``ProviderProfile``, or ``None``.

        Single base implementation that replaces the three identical per-client
        ``_profile_base_url`` classmethods (anthropic, openrouter, nvidia). A
        ``None`` return means "use the provider SDK/client default".

        Args:
            provider: Canonical provider string, e.g. ``"anthropic"``, ``"nvidia"``.

        Returns:
            URL string when the profile has one, ``None`` otherwise.
        """
        try:
            prof = get_profile(provider)
            if prof and prof.base_url:
                return prof.base_url
        except Exception:
            pass
        return None

    def _validate_config(self) -> None:
        """Validate configuration."""
        if not self.config:
            raise ValueError("No configuration provided")

        llm_config = self.config.get_llm_config()
        if not llm_config:
            raise ValueError("No LLM configuration found")

    @property
    def required_config(self) -> Dict[str, str]:
        """Get required configuration keys."""
        return {
            'model_type': 'Model type/name',
            'temperature': 'Sampling temperature',
            'max_tokens': 'Maximum tokens for generation'
        }

    _skip_validate = False

    async def _initialize(self) -> None:
        """Initialize LLM client."""
        try:
            await self._setup_client()

            self._configure_from_model_registry()

            if not self._skip_validate:
                await fresh_credential(self)
                await self._validate_connection()
            self._initialized = True
            self.logger.info(f"✨ {self.name} initialized with model {self.model_type} (max_tokens: {self.max_tokens})")
        except Exception as e:
            self.logger.error(f"Failed to initialize {self.name}: {e}")
            raise

    async def _cleanup(self) -> None:
        """Clean up LLM client resources."""
        try:
            await self._cleanup_client()
            self.logger.info(f"{self.name} cleaned up successfully")
        except Exception as e:
            self.logger.error(f"Failed to clean up {self.name}: {e}")
            raise

    @abstractmethod
    async def generate_response(
        self,
        messages: Optional[List[Dict[str, str]]] = None,
        system: Optional[str] = None,
        prompt: Optional[Union[str, Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """Generate a response from the LLM."""
        pass

    async def ensure_initialized(self) -> None:
        """Ensure client is initialized."""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    await self.initialize()

    def count_tokens(self, text: Union[str, List[Dict[str, Any]]]) -> int:
        """Count tokens using the centralized token counter.
        
        Args:
            text: Text or message list to count tokens for
            
        Returns:
            Number of tokens
        """
        model_name = self.model_type or "gpt-3.5-turbo"  # Fallback
        
        if isinstance(text, list):
            # Handle message list
            return count_messages_tokens(text, model_name)
        else:
            # Handle plain text
            return count_tokens(text, model_name)

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate tokens for a list of messages.
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Estimated token count
        """
        model_name = self.model_type or "gpt-3.5-turbo"  # Fallback
        return count_messages_tokens(messages, model_name)

    def get_context_window(self) -> int:
        """Get the context window size for this model.
        
        Returns:
            Context window size in tokens
        """
        return get_model_config(self.model_type).context_window

    def get_max_completion_tokens(self) -> int:
        """Get the maximum completion tokens for this model from registry.
        
        Single source of truth: model_registry.
        
        Returns:
            Maximum completion tokens for this model
        """
        return get_model_config(self.model_type).max_completion_tokens

    def _adjust_max_tokens(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        *,
        estimated_input_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """Clamp max_tokens to stay within model limits.

        Single canonical implementation shared by all providers.  Previously
        duplicated in DeepSeekClient, OpenRouterClient, and as inline blocks
        in OpenAI/Anthropic/Gemini clients.

        Args:
            messages: Message list used to estimate input tokens when
                ``estimated_input_tokens`` is not provided.
            max_tokens: Requested output token budget.  Falls back to
                ``self.max_tokens`` when None.
            estimated_input_tokens: Pre-computed input token count (keyword
                only).  Pass this when the call site has already added system-
                message tokens to the count (Anthropic / Gemini pattern) so
                the estimate is not double-counted.
            tools: The request's tool schemas, which the provider bills as input.

        Returns:
            Adjusted max_tokens value that fits within both the model's
            per-call completion-token limit and the available context window.

        Raises:
            LLMContextLengthError: The input leaves no room for a usable answer, so
                the call is not paid for.
        """
        max_tokens_value = max_tokens if max_tokens is not None else self.max_tokens

        # Absolute per-request output cap (cost + credit pre-auth guard). Binds
        # before the model/context clamps so a huge model ceiling never leaks out.
        cap = _output_token_cap()
        if max_tokens_value > cap:
            max_tokens_value = cap

        # Determine input token count. The step's provider-anchored count already
        # holds the tool schemas and everything the provider billed last time.
        anchored = anchored_input_tokens()
        if anchored is not None:
            input_tokens = anchored
        else:
            if estimated_input_tokens is not None:
                input_tokens = estimated_input_tokens
            else:
                # No tokenizer: this sync clamp runs on the event loop, and the step's
                # anchor already covers a real step.
                input_tokens = count_messages_tokens(messages, self.model_type, encode=False)
            input_tokens += count_tools_tokens(tools)

        context_window = self.get_context_window()
        max_completion = self.get_max_completion_tokens()

        # First, ensure max_tokens stays within the model's completion token limit
        if max_tokens_value > max_completion:
            self.logger.info(
                f"Adjusted max_tokens from {max_tokens_value} to {max_completion} "
                f"to respect model's completion token limit"
            )
            max_tokens_value = max_completion

        # Then, ensure the total (input + output) stays within the context window
        room = context_window - input_tokens - 100
        if room < min(max_tokens_value, _MIN_USEFUL_OUTPUT_TOKENS):
            raise LLMContextLengthError(
                f"context length: about {input_tokens} input tokens leave {max(room, 0)} of "
                f"{context_window} for model {self.model_type}")
        adjusted = min(max_tokens_value, room)

        if adjusted < max_tokens_value:
            self.logger.info(
                f"Adjusted max_tokens from {max_tokens_value} to {adjusted} "
                f"to stay within context window"
            )
            max_tokens_value = adjusted

        return max_tokens_value

    # Abstract methods that must be implemented by subclasses
    @abstractmethod
    async def _setup_client(self) -> None:
        """Set up the specific LLM client."""
        pass

    @abstractmethod
    async def _validate_connection(self) -> None:
        """Validate connection to LLM service."""
        pass

    @abstractmethod
    async def _cleanup_client(self) -> None:
        """Clean up the specific LLM client."""
        pass

    @abstractmethod
    async def _generate(
        self,
        prompt: Optional[Union[str, Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        system: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Union[str, AsyncGenerator[str, None]]:
        """Generate text from the LLM."""
        pass

    def _normalize_prompt(
        self,
        prompt: Optional[Union[str, Dict[str, Any], List[Dict[str, Any]]]],
        messages: Optional[List[Dict[str, Any]]],
        system: Optional[str] = None,
        *,
        hoist_dict_system: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Resolve the polymorphic (prompt, messages) input into one message list.

        Shared normalization for every provider client's
        ``generate_response``. Precedence: explicit ``messages`` > list prompt
        (all dicts) > dict prompt with ``'messages'`` > string prompt > default
        ``"Hello"`` user message.

        Returns ``(formatted_messages, system)``:

        - ``formatted_messages`` is the SAME list object that was passed in
          where possible (callers may mutate it, e.g. insert a system message
          at index 0 — the OpenAI-family pattern).
        - ``system`` is passed through unchanged, EXCEPT when
          ``hoist_dict_system=True`` and ``prompt`` is a dict carrying
          ``'system'`` while ``system`` is falsy — then the dict's value is
          hoisted (the Anthropic/Gemini pattern, where system rides a separate
          kwarg instead of the messages list).
        """
        if messages is not None:
            # Use messages directly if provided
            formatted_messages = messages
        elif isinstance(prompt, list) and all(isinstance(m, dict) for m in prompt):
            # Prompt is already a list of messages
            formatted_messages = prompt
        elif isinstance(prompt, dict) and 'messages' in prompt:
            # Extract messages (and optionally system) from prompt dict
            formatted_messages = prompt['messages']
            if hoist_dict_system and 'system' in prompt and not system:
                system = prompt['system']
        elif isinstance(prompt, str):
            # Convert string prompt to message format
            formatted_messages = [{"role": "user", "content": prompt}]
        else:
            # Default to empty message
            formatted_messages = [{"role": "user", "content": "Hello"}]
        return formatted_messages, system

    async def _generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Union[str, Tuple[str, List[Dict[str, Any]]]]:
        """Generate response with tools support.
        
        Default implementation that raises NotImplementedError.
        Subclasses that support tool-based generation should override this.
        """
        raise NotImplementedError("Tool-based generation not supported by this LLM client")

    def update_settings(self, settings: Dict[str, Any]) -> bool:
        """Update client settings.
        
        Handles temperature, max_tokens, model_type, and arbitrary attributes.
        
        Args:
            settings: Dictionary of settings to update
            
        Returns:
            bool: True if settings were updated successfully
        """
        try:
            # Track what was updated for logging
            updated = {}
            
            for key, value in settings.items():
                if key == 'temperature':
                    # Temperature with validation
                    try:
                        temp = float(value)
                        if 0.0 <= temp <= 2.0:  # Extended range for some models
                            self.temperature = temp
                            updated['temperature'] = temp
                        else:
                            self.logger.warning(f"Temperature value {temp} outside valid range (0.0-2.0)")
                    except (ValueError, TypeError) as e:
                        self.logger.error(f"Invalid temperature value: {e}")
                        
                elif key == 'max_tokens':
                    # Max tokens with validation
                    try:
                        tokens = int(value)
                        if tokens > 0:
                            self.max_tokens = tokens
                            updated['max_tokens'] = tokens
                        else:
                            self.logger.warning(f"Invalid max_tokens value: {tokens} (must be > 0)")
                    except (ValueError, TypeError) as e:
                        self.logger.error(f"Invalid max_tokens value: {e}")
                        
                elif key in ('model', 'model_type'):
                    # Model type - reconfigure from registry
                    if value:
                        old_model = self.model_type
                        self.model_type = value
                        updated['model_type'] = value
                        self.logger.info(f"Updated model: {old_model} → {value}")
                        
                        # Reconfigure from model registry with new model
                        self._configure_from_model_registry()
                        
                        if hasattr(self, 'supports_vision'):
                            self.supports_vision = self._resolve_supports_vision()
                            
                elif hasattr(self, key):
                    # Generic attribute update
                    prev_val = getattr(self, key)
                    setattr(self, key, value)
                    updated[key] = value
                    self.logger.debug(f"Updated {key} = {value} (was {prev_val})")
                else:
                    self.logger.warning(f"Attribute {key} not found, skipping")
            
            # Log summary of updates
            if updated:
                self.logger.info(f"Updated settings for {self.name}: {updated}")
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error updating settings: {e}")
            return False
    
    def get_settings(self) -> Dict[str, Any]:
        """Get current client settings.
        
        Returns:
            Dictionary of current settings
        """
        model_config = get_model_config(self.model_type)
        return {
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'name': self.name,
            'model': getattr(self, 'model_type', 'unknown'),
            'context_window': model_config.context_window,
            'max_completion_tokens': model_config.max_completion_tokens,
            'provider': model_config.provider.value,
            'chars_per_token': model_config.chars_per_token,
            'pricing': {
                'input_price': model_config.pricing.input_price,
                'output_price': model_config.pricing.output_price
            },
        }

    # ==========================================================================
    # CLIENT VALIDATION METHODS (Jan 4, 2026) - Consolidated from individual clients
    # Template method pattern for consistent validation across all providers.
    # ==========================================================================

    async def validate(self) -> bool:
        """Validate the LLM client by making a test request.

        Uses template method pattern - subclasses implement:
        - _make_validation_request(): Provider-specific API call
        - _check_validation_response(response): Provider-specific response check

        Returns:
            True if validation succeeds

        Raises:
            LLMAuthenticationError: If API key is invalid
            LLMRateLimitError: If rate limit exceeded
            LLMError: For other validation failures
        """
        await fresh_credential(self)
        provider = self.provider_label

        try:
            response = await self._make_validation_request()
            self._check_validation_response(response)
            self.logger.info(f"{provider} client validated successfully with model {self.model_type}")
            return True
        except LLMError:
            raise  # Already translated
        except Exception as e:
            translated = translate_llm_error(e, f"{provider} validation", model=self.model_type)
            self.logger.error(f"{provider} validation failed: {e}")
            raise translated

    @abstractmethod
    async def _make_validation_request(self) -> Any:
        """Make a minimal test request to validate the client.

        Must be implemented by subclasses. Should make a minimal API call
        (e.g., 1 token max_tokens) to verify connectivity and authentication.

        Returns:
            Provider-specific response object
        """
        pass

    @abstractmethod
    def _check_validation_response(self, response: Any) -> None:
        """Validate the response from the test request.

        Must be implemented by subclasses. Should raise ValueError if
        the response is invalid or missing expected content.

        Args:
            response: Provider-specific response object

        Raises:
            ValueError: If response is invalid
        """
        pass

    # ==========================================================================
    # TOOL VALIDATION METHODS
    # These methods provide consistent tool schema validation across all providers.
    # ==========================================================================
    
    def _validate_tool_schema(self, tool: Union[Dict[str, Any], Any]) -> bool:
        """Validate tool schema before API call.
        
        Validates tools in OpenAI format (standard) or provider-specific formats.
        Override in subclasses for provider-specific validation.
        
        Args:
            tool: Tool definition (dict or tool object)
            
        Returns:
            True if valid, False otherwise
        """
        try:
            # Handle tool objects with arg schemas
            if not isinstance(tool, dict):
                if hasattr(tool, 'name') and tool.name:
                    return True
                self.logger.error(f"Tool object missing required 'name' attribute")
                return False
            
            # OpenAI format: {"type": "function", "function": {...}}
            if tool.get('type') == 'function':
                func = tool.get('function', {})
                if not func.get('name'):
                    self.logger.error(f"Tool function missing required 'name' field")
                    return False
                return True
            
            # Direct format: {"name": ..., "description": ..., "parameters": ...}
            if 'name' in tool:
                if not tool['name']:
                    self.logger.error(f"Tool missing required 'name' value")
                    return False
                return True
            
            # Gemini format: {"function_declarations": [...]}
            if 'function_declarations' in tool:
                funcs = tool['function_declarations']
                if not isinstance(funcs, list) or len(funcs) == 0:
                    self.logger.error(f"Invalid function_declarations: must be non-empty list")
                    return False
                for func in funcs:
                    if not isinstance(func, dict) or not func.get('name'):
                        self.logger.error(f"Function declaration missing required 'name' field")
                        return False
                return True
            
            self.logger.error(f"Unknown tool format: {list(tool.keys())}")
            return False
            
        except Exception as e:
            self.logger.error(f"Tool validation error: {e}")
            return False

