"""Base LLM client implementation."""

import asyncio
import logging
import os
from typing import Optional, List, Dict, AsyncGenerator, Any, Union, TYPE_CHECKING, Tuple
from abc import ABC, abstractmethod

if TYPE_CHECKING:
    from modules.base_module import BaseModule
    from core.config import BotConfig

from core.exceptions import (
    LLMError,
    LLMConfigError,
    LLMConnectionError,
    LLMResponseError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMInvalidRequestError,
    LLMPermanentError,
)

# Runtime imports
from modules.base_module import BaseModule
from core.config import BotConfig

# FIXED: Import model registry and token counter for intelligent configuration
from modules.llm.model_registry import get_model_config
from modules.llm.token_counter import count_tokens, count_messages_tokens
from modules.llm.profiles import get_profile


def translate_llm_error(error: Exception, context: str = "") -> Exception:
    """Unified LLM error classifier — single source of truth.

    Maps common error patterns (drawn from the union of all provider-side
    classifiers) to the appropriate LLMError subclass.  Callers should
    *raise* the returned exception; this function only constructs it.

    Priority order (earlier wins):
      1. Permanent / billing  — must NOT be retried (402, insufficient_quota …)
      2. Rate-limit           — retriable after back-off
      3. Authentication       — usually permanent but handled separately
      4. Context-length       — request must be shortened
      5. Invalid-request      — bad payload
      6. Connection / timeout — retriable network error
      7. Generic LLMError     — fallback

    Token sets are the UNION of llm_client, adapters, openrouter, and gemini
    classifiers as they existed before this consolidation.
    """
    error_str = str(error).lower()

    # 1. Permanent / billing errors — halt, do NOT fall through to rate-limit
    #    (adapters.py was the only previous carrier of this branch)
    if any(x in error_str for x in [
        'insufficient_quota', 'billing', 'account_deactivated', 'suspended', '402',
    ]):
        return LLMPermanentError(f"{context}: {error}" if context else str(error))

    # 2. Rate-limit errors
    #    Union: base had 'rate_limit'/'rate limit'/'too many requests'/'429';
    #    adapters also had 'quota'; openrouter had 'rate limit'/'429' (bare 'rate'
    #    dropped — it caused false positives on "moderate", "accurate", etc.);
    #    gemini validation had 'Resource exhausted'.
    if any(x in error_str for x in [
        'rate_limit', 'rate limit', 'too many requests', '429', 'quota',
        'resource exhausted',
    ]):
        return LLMRateLimitError(f"{context}: {error}" if context else str(error))

    # 3. Authentication errors
    #    Union: base had 'authentication'/'auth'/'api_key'/'api key'/'unauthorized'/'401';
    #    adapters also had 'invalid key'; openrouter had 'key'/'auth'/'401'.
    if any(x in error_str for x in [
        'authentication', 'auth', 'unauthorized', '401', 'api_key', 'api key', 'invalid key',
    ]):
        return LLMAuthenticationError(f"{context}: {error}" if context else str(error))

    # 4. Context-length errors
    #    NOTE: bare 'tokens' was removed — it false-matched unrelated messages like
    #    "insufficient tokens" / "50 tokens remaining" / "invalid tokens in request",
    #    misrouting them to history-shrink recovery instead of billing/failover.
    if any(x in error_str for x in [
        'context_length', 'context length', 'too long', 'maximum context',
        'token limit', 'maximum tokens', 'too many tokens',
    ]):
        return LLMContextLengthError(f"{context}: {error}" if context else str(error))

    # 5. Invalid-request errors
    if any(x in error_str for x in ['invalid', 'bad request', '400', 'malformed']):
        return LLMInvalidRequestError(f"{context}: {error}" if context else str(error))

    # 6. Connection / network errors
    #    Union: base; gemini had 'resource exhausted' (Gemini-specific rate signal handled
    #    separately in the validation path which returns rather than raises).
    if any(x in error_str for x in [
        'connection', 'network', 'timeout', 'unreachable',
    ]):
        return LLMConnectionError(f"{context}: {error}" if context else str(error))

    # 7. Generic fallback
    return LLMError(f"{context}: {error}" if context else str(error))


_DEFAULT_OUTPUT_TOKEN_CAP = 16384


def _output_token_cap() -> int:
    """Absolute per-request completion-token ceiling (env ``LLM_MAX_OUTPUT_TOKENS``).

    Models advertise huge completion limits (e.g. GLM-5.2 = 262144). Requesting
    the full ceiling wastes cost and makes credit-metered providers (OpenRouter)
    pre-authorize the entire amount, producing spurious HTTP 402s when the balance
    can't cover it. Cap every request to a sane default; override per-deployment.
    """
    try:
        v = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", str(_DEFAULT_OUTPUT_TOKEN_CAP)))
        return v if v > 0 else _DEFAULT_OUTPUT_TOKEN_CAP
    except (TypeError, ValueError):
        return _DEFAULT_OUTPUT_TOKEN_CAP


class LLMClient(BaseModule):
    """Base class for LLM clients."""

    # UP-08: resolved prompt-cache strategy, stamped by create_chat_model. "none" until
    # stamped so any client that bypasses the factory is back-compatible.
    cache_strategy: str = "none"

    # ADDED (Nov 25, 2025): Centralized timeout/retry constants
    # Use these across all clients and llm_factory for consistency
    # FIX (Dec 31, 2025): These are now sourced from TimeoutConfig in agents/task/constants.py
    # The values here serve as fallbacks for compatibility
    DEFAULT_REQUEST_TIMEOUT = 120  # Canonical source: TimeoutConfig.LLM_REQUEST_TIMEOUT
    DEFAULT_MAX_RETRIES = 3

    # FIXED: Update default parameters to use model registry
    DEFAULT_MAX_TOKENS = {
        'default': 8000,  # Conservative default increased from 512
        'summary': 1000,  # Increased from 256 for better summaries
        'chat': 2000,     # Increased from 512 for better conversations
        'analysis': 4000  # Increased from 768 for detailed analysis
    }

    DEFAULT_TEMPERATURES = {
        'default': 0.5,
        'factual': 0.2,
        'creative': 0.7,
        'analysis': 0.3
    }

    def __init__(self, config: BotConfig, name: str = "llm_client", container=None):
        """Initialize LLM client."""
        super().__init__(name=name, config=config, container=container)
        self.model_type = None  # Will be set by implementations
        self._retries = 0
        self._max_retries = config.get('max_retries', 3)
        self._retry_delay = config.get('retry_delay', 1)
        self.api_key = None
        
        # FIXED: Initialize with intelligent defaults that will be updated by model registry
        self.temperature = 0.7
        self.max_tokens = 8000  # Conservative default, will be updated by registry
        
        # Validate config if needed
        self._validate_config()

    def _get_model_config(self):
        """Get model configuration from registry if available."""
        if self.model_type:
            return get_model_config(self.model_type)
        return None

    def _configure_from_model_registry(self) -> None:
        """Configure client parameters from model registry."""
        try:
            model_config = self._get_model_config()
            if model_config:
                # Set max_tokens based on model's actual capabilities
                if model_config.max_completion_tokens and model_config.max_completion_tokens > 0:
                    self.max_tokens = model_config.max_completion_tokens
                    self.logger.info(f"Set max_tokens to {self.max_tokens} from model registry for {self.model_type}")
                
                # Log model capabilities for debugging
                self.logger.debug(f"Model {self.model_type} configured: context={model_config.context_window}, completion={model_config.max_completion_tokens}")
            else:
                self.logger.debug(f"Model {self.model_type} not found in registry, using defaults")
        except Exception as e:
            self.logger.warning(f"Failed to configure from model registry: {e}")

    # ------------------------------------------------------------------
    # Task 2.4: shared helpers de-duplicated from per-provider clients
    # ------------------------------------------------------------------

    def _resolve_supports_vision(self, model_type: Optional[str] = None) -> bool:
        """Resolve whether a model supports vision.

        Consults the model registry; falls back to ``True`` when the model is
        not registered (matching the behaviour of openai/anthropic/gemini/
        openrouter clients: "assume vision if not in registry").  Clients whose
        un-registered fallback should be ``False`` (nvidia) must override this
        method or skip it and set ``self.supports_vision`` directly.

        Args:
            model_type: Model name to look up.  Defaults to ``self.model_type``.

        Returns:
            True when vision is supported (or unknown); False otherwise.
        """
        name = model_type if model_type is not None else self.model_type
        try:
            mc = get_model_config(name)
            if mc is not None:
                return mc.capabilities.supports_vision
        except Exception:
            pass
        return True  # Assume vision support if not in registry

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

    def get_max_safe_input_tokens(self) -> int:
        """Get the maximum safe input token count for this model.
        
        Returns:
            Maximum safe input tokens (context window - completion tokens)
        """
        model_config = self._get_model_config()
        if model_config:
            return model_config.safe_input_tokens
        else:
            # Fallback calculation
            estimated_context = 16000  # Conservative estimate
            return estimated_context - self.max_tokens

    def get_context_window(self) -> int:
        """Get the context window size for this model.
        
        Returns:
            Context window size in tokens
        """
        model_config = self._get_model_config()
        if model_config:
            return model_config.context_window
        else:
            return 16000  # Conservative default

    def get_max_completion_tokens(self) -> int:
        """Get the maximum completion tokens for this model from registry.
        
        REFACTORED (Nov 25, 2025): Single source of truth from model_registry.
        
        Returns:
            Maximum completion tokens for this model
        """
        model_config = self._get_model_config()
        if model_config and model_config.max_completion_tokens:
            return model_config.max_completion_tokens
        return 8192  # Conservative default
    
    def get_model_limits(self) -> Dict[str, int]:
        """Get all token limits for this model from registry.

        REFACTORED (Nov 25, 2025): Consolidates limit retrieval to single method.

        Returns:
            Dict with context_window, max_completion_tokens, safe_input_tokens
        """
        model_config = self._get_model_config()
        if model_config:
            return {
                'context_window': model_config.context_window,
                'max_completion_tokens': model_config.max_completion_tokens,
                'safe_input_tokens': model_config.safe_input_tokens
            }
        else:
            # Conservative defaults
            return {
                'context_window': 16000,
                'max_completion_tokens': 8192,
                'safe_input_tokens': 7800
            }

    # ADDED (Dec 13, 2025): Underscore-prefixed aliases for subclass compatibility
    # These delegate to the public methods - subclasses no longer need to override
    def _get_context_window(self) -> int:
        """Alias for get_context_window() - for subclass compatibility."""
        return self.get_context_window()

    def _get_max_completion_tokens(self) -> int:
        """Alias for get_max_completion_tokens() - for subclass compatibility."""
        return self.get_max_completion_tokens()

    def _adjust_max_tokens(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        *,
        estimated_input_tokens: Optional[int] = None,
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

        Returns:
            Adjusted max_tokens value that fits within both the model's
            per-call completion-token limit and the available context window.
        """
        max_tokens_value = max_tokens if max_tokens is not None else self.max_tokens

        # Absolute per-request output cap (cost + credit pre-auth guard). Binds
        # before the model/context clamps so a huge model ceiling never leaks out.
        cap = _output_token_cap()
        if max_tokens_value > cap:
            max_tokens_value = cap

        # Determine input token count
        if estimated_input_tokens is not None:
            input_tokens = estimated_input_tokens
        else:
            input_tokens = count_messages_tokens(messages, self.model_type)

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
        adjusted = max(1, min(max_tokens_value, context_window - input_tokens - 100))

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

    def _format_prompt(self, prompt_bundle: Union[str, Dict[str, Any], List[Dict[str, str]]]) -> Dict[str, Any]:
        """Format prompt for specific LLM implementation."""
        try:
            if isinstance(prompt_bundle, str):
                return {"messages": [{"role": "user", "content": prompt_bundle}]}
            
            elif isinstance(prompt_bundle, list):
                return {"messages": prompt_bundle}
            
            elif isinstance(prompt_bundle, dict):
                messages = []
                
                # Handle system prompt
                if "system" in prompt_bundle:
                    messages.append({
                        "role": "system",
                        "content": prompt_bundle["system"]
                    })
                    
                # Handle message list
                if "messages" in prompt_bundle:
                    messages.extend(prompt_bundle["messages"])
                    
                # Handle character integration
                if "character" in prompt_bundle and prompt_bundle["character"]:
                    char_info = prompt_bundle["character"]
                    if not any(msg["role"] == "system" for msg in messages):
                        char_prompt = f"You are {char_info.get('name', 'an AI assistant')}.\n"
                        if char_info.get('bio'):
                            char_prompt += f"\n{char_info['bio']}\n"
                        messages.insert(0, {
                            "role": "system",
                            "content": char_prompt
                        })
                        
                return {"messages": messages}
                
            else:
                raise ValueError(f"Unsupported prompt type: {type(prompt_bundle)}")
            
        except Exception as e:
            raise LLMConfigError(f"Error formatting prompt: {str(e)}")

    async def generate_agent_response(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Union[str, Tuple[str, List[Dict[str, Any]]]]:
        """Generate a response specifically for agent interactions with tool support."""
        try:
            await self.ensure_initialized()
            
            formatted_messages = []
            
            # Handle system message
            if system:
                formatted_messages.append({
                    "role": "system",
                    "content": system
                })
            
            # Format and validate messages
            if messages:
                if isinstance(messages, list):
                    for msg in messages:
                        if not isinstance(msg, dict) or "role" not in msg:
                            raise LLMConfigError(f"Invalid message format: {msg}")
                        formatted_messages.append(msg)
                else:
                    formatted = self._format_prompt(messages)
                    formatted_messages.extend(formatted["messages"])
            
            # Validate tools format if provided
            if tools:
                for tool in tools:
                    if not isinstance(tool, dict) or "type" not in tool:
                        raise LLMConfigError(f"Invalid tool format: {tool}")
            
            # Try tool-based generation if supported
            try:
                response = await self._generate_with_tools(
                    messages=formatted_messages,
                    tools=tools,
                    metadata=metadata,
                    **kwargs
                )
                return response
            except NotImplementedError:
                # Fall back to regular generation if tools not supported
                self.logger.warning(f"{self.__class__.__name__} does not support tool-based generation, falling back to regular generation")
                return await self.generate_response(
                    messages=formatted_messages,
                    metadata=metadata,
                    **kwargs
                )
            
        except LLMError:
            raise
        except Exception as e:
            self.logger.error(f"Error generating agent response: {str(e)}")
            raise LLMError(f"Agent response generation failed: {str(e)}")

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
        
        REFACTORED (Nov 25, 2025): Consolidated from subclass implementations.
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
                        
                        # Update supports_vision if model capabilities change
                        model_config = self._get_model_config()
                        if model_config and hasattr(self, 'supports_vision'):
                            self.supports_vision = model_config.capabilities.supports_vision
                            
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
        model_config = self._get_model_config()
        
        result = {
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'name': self.name,
            'model': getattr(self, 'model_type', 'unknown')
        }
        
        # Add model registry information if available
        if model_config:
            result.update({
                'context_window': model_config.context_window,
                'max_completion_tokens': model_config.max_completion_tokens,
                'provider': model_config.provider.value,
                'chars_per_token': model_config.chars_per_token,
                'pricing': {
                    'input_price': model_config.pricing.input_price,
                    'output_price': model_config.pricing.output_price
                }
            })
        
        return result

    # ==========================================================================
    # ERROR HANDLING METHODS (Nov 25, 2025) - Standardized error translation
    # These methods provide consistent error handling across all providers.
    # ==========================================================================
    
    def _translate_error(self, error: Exception, context: str = "") -> Exception:
        """Translate provider-specific errors to standard LLM exceptions.

        Delegates to the module-level ``translate_llm_error`` function which is
        the single source of truth for error classification across all providers.
        Override in subclasses only for provider-specific quirks not covered by
        the base classifier.

        Args:
            error: The original exception
            context: Additional context about where the error occurred

        Returns:
            Appropriate LLMError subclass
        """
        return translate_llm_error(error, context)
    
    async def _safe_api_call(
        self, 
        api_func, 
        *args, 
        context: str = "API call",
        **kwargs
    ) -> Any:
        """Safely execute an API call with error translation.
        
        Wraps API calls with consistent error handling and translation.
        
        Args:
            api_func: Async function to call
            *args: Positional arguments for the function
            context: Context string for error messages
            **kwargs: Keyword arguments for the function
            
        Returns:
            Result of the API call
            
        Raises:
            Appropriate LLMError subclass
        """
        try:
            return await api_func(*args, **kwargs)
        except LLMError:
            # Re-raise LLM errors as-is
            raise
        except Exception as e:
            # Translate to appropriate LLM error
            translated = self._translate_error(e, context)
            self.logger.error(f"{context} failed: {e}")
            raise translated

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
        provider = self.__class__.__name__.replace('Client', '')

        try:
            response = await self._make_validation_request()
            self._check_validation_response(response)
            self.logger.info(f"{provider} client validated successfully with model {self.model_type}")
            return True
        except LLMError:
            raise  # Already translated
        except Exception as e:
            translated = self._translate_error(e, f"{provider} validation")
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
    # TOOL VALIDATION METHODS (Nov 25, 2025) - Consolidated from individual clients
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

    def _validate_tools_list(self, tools: Optional[List[Any]]) -> List[Dict[str, Any]]:
        """Validate and filter a list of tools.
        
        Args:
            tools: List of tool definitions
            
        Returns:
            List of valid tools
        """
        if not tools:
            return []
        
        valid_tools = []
        for i, tool in enumerate(tools):
            if self._validate_tool_schema(tool):
                valid_tools.append(tool)
            else:
                self.logger.warning(f"Skipping invalid tool at index {i}")
        
        return valid_tools

    # ==========================================================================
    # TELEMETRY METHODS (Nov 25, 2025) - Consolidated from individual clients
    # These methods provide consistent telemetry handling across all providers.
    # ==========================================================================
    
    def _safe_int_extract(self, value: Any) -> Optional[int]:
        """Safely extract integer from various value types.
        
        Handles int, float, string conversions robustly.
        
        Args:
            value: Value to convert to int
            
        Returns:
            Integer value or None if conversion fails
        """
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            if value.lower() in ['none', 'null', '']:
                return None
            try:
                return int(float(value))  # Handle "123.0" strings
            except (ValueError, TypeError):
                return None
        return None

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Extract usage data from last_response.
        
        Override in subclasses to handle provider-specific response formats.
        
        Returns:
            Dict with prompt_tokens, completion_tokens, total_tokens.
            Returns None values if usage data not available.
        """
        last_response = getattr(self, 'last_response', None)
        
        if not last_response:
            self.logger.debug("No last_response available for usage extraction")
            return {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}
        
        # Default extraction for OpenAI-compatible responses
        if isinstance(last_response, dict) and 'usage' in last_response:
            usage = last_response['usage']
            prompt_tokens = usage.get('prompt_tokens')
            completion_tokens = usage.get('completion_tokens')
            total_tokens = usage.get('total_tokens')
            
            # Calculate total if not provided
            if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens
            
            return {
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens
            }
        
        return {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None}

    def _generate_request_id(self, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Generate unique request ID with consistent format.
        
        Args:
            metadata: Optional metadata containing session/agent info
            
        Returns:
            Unique request ID string
        """
        import uuid
        import time
        
        # Include timestamp for uniqueness and debugging
        timestamp_ms = int(time.time() * 1000)
        
        # Include session/agent info if available
        session_part = ""
        if metadata:
            session_id = metadata.get('session_id')
            agent_id = metadata.get('agent_id')
            if session_id:
                session_part = f"_{str(session_id)[:8]}"
            elif agent_id:
                session_part = f"_{str(agent_id)[:8]}"
        
        return f"llm_{timestamp_ms}{session_part}_{uuid.uuid4().hex[:8]}"

    def _extract_usage_and_capture_telemetry(
        self, 
        start_time: float, 
        success: bool, 
        error: Optional[str] = None, 
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Extract token usage and capture telemetry with comprehensive fallback logic.
        
        Consolidated implementation (Dec 13, 2025) that handles all fallback scenarios:
        1. Calculate total from components if missing
        2. Estimate from response content if no usage data
        3. Use minimum viable tokens as last resort
        
        Subclasses only need to override _extract_usage_data() for provider-specific
        response parsing - the fallback logic is centralized here.
        
        Args:
            start_time: Request start time (from time.time())
            success: Whether the request succeeded
            error: Error message if request failed
            metadata: Optional request metadata
        """
        import time
        
        try:
            duration_seconds = time.time() - start_time
            
            # Extract usage data (provider-specific, may return None values)
            usage_data = self._extract_usage_data()
            prompt_tokens = usage_data.get('prompt_tokens')
            completion_tokens = usage_data.get('completion_tokens')
            total_tokens = usage_data.get('total_tokens')
            cached_tokens = usage_data.get('cached_tokens', 0)
            
            # Fallback 1: Calculate total from components
            if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens
                self.logger.debug(f"Calculated total_tokens from components: {total_tokens}")
            
            # Fallback 2: Estimate from response content
            if total_tokens is None:
                estimated_tokens = self._estimate_tokens_from_response()
                if estimated_tokens > 0:
                    total_tokens = estimated_tokens
                    completion_tokens = int(estimated_tokens * 0.3)  # Rough split
                    prompt_tokens = total_tokens - completion_tokens
                    self.logger.info(f"Estimated tokens: prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens}")
            
            # Fallback 3: Minimum viable tokens (better than None)
            if total_tokens is None:
                total_tokens = 100  # Conservative minimum
                prompt_tokens = 70
                completion_tokens = 30
                self.logger.warning(f"Using minimum token estimate: {total_tokens}")
            
            # Extract session/agent info for logging
            session_id = None
            agent_id = None
            if metadata:
                session_id = metadata.get('session_id')
                agent_id = metadata.get('agent_id')
            
            # Generate request ID
            request_id = self._generate_request_id(metadata)
            
            # Log telemetry with cached token info if present
            provider = self.__class__.__name__.replace('Client', '')
            if cached_tokens and cached_tokens > 0:
                self.logger.debug(
                    f"{provider} request completed: model={self.model_type}, tokens={total_tokens} "
                    f"(cached={cached_tokens}), duration={duration_seconds:.2f}s, success={success}"
                )
            else:
                self.logger.debug(
                    f"{provider} request completed: model={self.model_type}, tokens={total_tokens}, "
                    f"duration={duration_seconds:.2f}s, success={success}"
                )
            
        except Exception as e:
            self.logger.debug(f"Error in _extract_usage_and_capture_telemetry: {e}")
    
    def _estimate_tokens_from_response(self) -> int:
        """Estimate tokens from response content when usage data unavailable.
        
        Base implementation - subclasses can override for provider-specific
        response structures.
        
        Returns:
            Estimated token count or 0 if estimation fails
        """
        try:
            from modules.llm.token_counter import count_tokens
            
            last_response = getattr(self, 'last_response', None)
            if not last_response:
                return 0
            
            # Try common response structures
            content = None
            
            # OpenAI-style: response.choices[0].message.content
            if hasattr(last_response, 'choices') and last_response.choices:
                choice = last_response.choices[0]
                if hasattr(choice, 'message') and choice.message:
                    content = getattr(choice.message, 'content', None)
            
            # Anthropic-style: response.content (list of blocks)
            elif hasattr(last_response, 'content') and isinstance(last_response.content, list):
                parts = []
                for block in last_response.content:
                    if hasattr(block, 'text') and block.text:
                        parts.append(block.text)
                content = ' '.join(parts)
            
            # Gemini-style: response.text
            elif hasattr(last_response, 'text') and last_response.text:
                content = last_response.text
            
            if content and content.strip():
                return count_tokens(content, self.model_type or 'gpt-3.5-turbo')
                
        except Exception as e:
            self.logger.debug(f"Token estimation failed: {e}")
        
        return 0