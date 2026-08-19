"""LLM Manager for centralizing LLM client management."""

import logging
import asyncio
import os
from typing import Dict, Any, Optional, List, Tuple, Set, Union

# Native BaseChatModel
from modules.llm.adapters import BaseChatModel

from core.base_component import BaseComponent
from core.config import BotConfig
from core.container import DependencyContainer
from core.exceptions import LLMError, LLMConfigError, ServiceError

from modules.llm.llm_client import LLMClient

# NOTE: provider client classes (AnthropicClient/OpenAIClient/GeminiClient/…) are
# deliberately NOT imported at module level — each drags its vendor SDK (openai,
# anthropic, google.generativeai) into every container build. create_llm_client
# (llm_client_registry) imports the one class the active provider needs.

# Import from registry to avoid circular imports
from modules.llm.llm_client_registry import (
    AVAILABLE_MODELS, 
    DEFAULT_MODELS, 
    get_default_model,
    create_llm_client
)

# FIXED: Import model registry for intelligent token limits and configuration
from modules.llm.model_registry import get_model_config


def _redact_llm_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a log-safe copy of the LLM config: api_key values masked to
    ``<set>``/``<missing>``, all other (non-secret) fields preserved. Never
    mutates the input. Used so container-build logging can't print live keys.
    """
    redacted: Dict[str, Any] = {}
    for provider, data in (config or {}).items():
        if isinstance(data, dict):
            safe = dict(data)
            if "api_key" in safe:
                safe["api_key"] = "<set>" if safe.get("api_key") else "<missing>"
            redacted[provider] = safe
        else:
            redacted[provider] = data
    return redacted


def _provider_of(client_name: str) -> str:
    """Provider name behind a registered client name.

    ``_ensure_fallback_client`` registers 'openai_fallback_client', so stripping
    only the '_client' suffix yields the bogus provider 'openai_fallback' — no
    llm_config entry, no isolated client, silently skipped. This is the one
    derivation; ``get_available_models`` uses the same two-step strip.
    """
    return (client_name or '').replace('_client', '').replace('_fallback', '')


class LLMManager(BaseComponent):
    """Service for managing LLM clients and configurations."""

    @staticmethod
    def _provider_display(provider: str) -> str:
        """Human label for a provider name (profile display_name; degrades to the
        raw name — never ``str.title()``, which mangles OpenRouter/NVIDIA/user rows)."""
        try:
            from modules.llm.profiles import get_profile
            prof = get_profile(provider)
            if prof is not None and prof.display_name:
                return prof.display_name
        except Exception:
            pass
        return provider

    @staticmethod
    def _provider_unavailable_message(provider: str, available: list) -> str:
        """The remedy a user sees when a requested provider has no client.

        Must name remedies that work on an INSTALLED box (the old string pointed
        at repo-relative ``config/.env.<ENV>``, which does not exist for a pip
        install)."""
        return (
            f"LLM client for provider '{provider}' is not available. "
            f"Available providers: {', '.join(available) if available else 'none'}. "
            f"Run `polyrob doctor` to see credential state; set a key with "
            f"`polyrob init` or `polyrob config set <PROVIDER>_API_KEY <value>`, "
            f"or declare a custom endpoint in ~/.polyrob/providers.yaml."
        )

    def __init__(self, name: str, config: BotConfig, container: Optional[DependencyContainer] = None):
        """Initialize LLM Manager service."""
        super().__init__(name=name, config=config, container=container)
        self.clients: Dict[str, LLMClient] = {}  # Mapping of client_name to client instance
        self.primary_client_name: Optional[str] = None  # Name of the primary client
        self.fallback_client_name: Optional[str] = None  # Name of the fallback client
        self.llm_config = config.get_llm_config()
        self._client_initializing_lock = asyncio.Lock()
        self._fallback_enabled = True  # Always enable fallback
        self._initialization_attempts = {}  # Track initialization attempts
        
        # Fallback hierarchy — derived from the ProviderSpec registry (024 seam 10);
        # the legacy literal is the LLM_PROVIDER_REGISTRY=off kill-switch path.
        # NOTE: deepseek_client DISABLED - use OpenRouter's DeepSeek instead
        self.FALLBACK_HIERARCHY = self._build_fallback_hierarchy()

    @staticmethod
    def _build_fallback_hierarchy():
        """(client_name, model) fallback pairs.

        Derived: fallback-eligible specs ordered by their ``fallback_rank``
        (mirrors the legacy openai→anthropic→openrouter→gemini order exactly —
        pinned by the characterization suite), models from the DEFAULT_MODELS
        policy table (or the spec's own default for a providers.yaml row that
        opted into ``fallback_eligible: true``, appended after the built-ins).
        """
        legacy = [
            ('openai_client', 'gpt-5'),  # Ultimate fallback - GPT-5
            ('anthropic_client', 'claude-sonnet-4-5'),
            ('openrouter_client', 'z-ai/glm-5.2'),  # OpenRouter default = Z.AI GLM flagship
            ('gemini_client', 'gemini-2.5-flash'),
        ]
        try:
            from modules.llm.llm_client_registry import DEFAULT_MODELS
            from modules.llm.provider_spec import get_specs, provider_registry_enabled
            if not provider_registry_enabled():
                return legacy
            eligible = [s for s in get_specs() if s.fallback_eligible]
            ranked = sorted(
                (s for s in eligible if s.fallback_rank is not None),
                key=lambda s: s.fallback_rank,
            )
            unranked = [s for s in eligible if s.fallback_rank is None]
            out = []
            for s in ranked + unranked:
                model = s.default_model or DEFAULT_MODELS.get(s.name)
                if model:
                    out.append((f"{s.name}_client", model))
            return out or legacy
        except Exception:
            return legacy

    def _configure_client_token_limits(self, client: LLMClient, model_name: str) -> None:
        """Configure client with appropriate token limits from model registry.
        
        REFACTORED (Dec 2025): Delegates to client._configure_from_model_registry()
        to avoid duplicating the configuration logic. Only handles extra 
        manager-specific settings like underlying LLM and temperature defaults.
        
        Args:
            client: The LLM client to configure
            model_name: The model name to get configuration for
        """
        try:
            # Ensure model_type is set before calling configure
            if model_name and hasattr(client, 'model_type'):
                client.model_type = model_name
            
            # Delegate to client's own configuration method (SINGLE SOURCE OF TRUTH)
            if hasattr(client, '_configure_from_model_registry'):
                client._configure_from_model_registry()
            
            # Get model config for additional manager-specific settings
            model_config = get_model_config(model_name)
            
            if model_config:
                # Also set on underlying LLM if it exists (manager-specific)
                if hasattr(client, 'llm') and hasattr(client.llm, 'max_tokens'):
                    client.llm.max_tokens = model_config.max_completion_tokens
                    self.logger.debug(f"Set underlying LLM max_tokens to {model_config.max_completion_tokens}")
                    
            # Set default temperature if not already configured
            if hasattr(client, 'temperature') and not hasattr(client, '_temperature_set'):
                client.temperature = 0.7
                client._temperature_set = True
                    
        except Exception as e:
            self.logger.warning(f"Failed to configure client token limits for {model_name}: {e}")
                
    async def _initialize(self) -> None:
        """Initialize LLM manager."""
        try:
            # Initialize all available clients. The candidate set is the SINGLE
            # source of truth in profiles (ProviderProfile.initializable) — the same
            # oracle the CLI's should_warn_no_key / resolver / env-backfill read, so
            # "you have a usable key" and "what I'll try to init" can never drift.
            # deepseek is initializable=False (direct client disabled — tool calling
            # broken; use OpenRouter's deepseek/deepseek-chat).
            from modules.llm.profiles import PROFILES
            clients_to_try = [p.name for p in PROFILES.values() if p.initializable]
            initialized_clients = []
            initialization_results = []
            
            # Log a REDACTED view — never the raw config (it holds live api_keys,
            # and this fires on every container build, incl. on the CLI's stdout).
            self.logger.info(f"LLM config (providers): {_redact_llm_config(self.llm_config)}")
            
            # Always ensure OpenAI client for fallback
            await self._ensure_fallback_client()
            
            for client_name in clients_to_try:
                config_data = self.llm_config.get(client_name, {})
                self.logger.debug(f"Checking {client_name} config: {bool(config_data)}")
                
                if config_data and config_data.get('api_key'):
                    try:
                        # Get model type from config or use default from registry
                        model_type = config_data.get('model') or get_default_model(client_name)
                        
                        if not model_type:
                            self.logger.warning(f"No model type found for {client_name}")
                            continue
                        
                        # Check if client is already registered in container
                        service_name = f"{client_name}_client"
                        existing_client = None
                        
                        if self.container and self.container.has_service(service_name):
                            existing_client = self.container.get_service(service_name)
                            if existing_client and getattr(existing_client, '_initialized', False):
                                # Configure the existing client with proper token limits
                                self._configure_client_token_limits(existing_client, model_type)
                                
                                # Use existing client
                                self.clients[service_name] = existing_client
                                initialized_clients.append((service_name, existing_client))
                                self.logger.info(f"Using existing {self._provider_display(client_name)} LLM client from container")
                                continue
                        
                        # Create client instance with appropriate configuration
                        client = create_llm_client(
                            name=client_name,
                            config=self.config,
                            container=self.container,
                            model_type=model_type
                        )
                        
                        # Configure client with intelligent token limits from registry
                        self._configure_client_token_limits(client, model_type)
                        
                        # Initialize the client if needed
                        if not getattr(client, '_initialized', False):
                            await client.initialize()
                        
                        # Store the client
                        service_name = f"{client_name}_client"
                        self.clients[service_name] = client
                        initialized_clients.append((service_name, client))
                        
                        # Register in container if not already there
                        if self.container and not self.container.has_service(service_name):
                            self.container.register_service(service_name, client, is_optional=True)
                        
                        initialization_results.append((client_name, True, None))
                        self.logger.info(f"✅ {self._provider_display(client_name)} LLM client initialized with model {model_type}")
                        
                    except Exception as e:
                        initialization_results.append((client_name, False, str(e)))
                        self.logger.warning(f"❌ Failed to initialize {client_name} client: {e}")
                        continue
                else:
                    self.logger.debug(f"⚠️ {self._provider_display(client_name)} LLM client not configured (missing config or API key)")

            # Set primary client based on config preference or first available
            await self._set_primary_client()
            
            # Set fallback client
            await self._set_fallback_client()
            
            # Log summary
            self.logger.info(f"LLM Manager initialized with {len(self.clients)} clients")
            self.logger.info(f"Primary client: {self.primary_client_name}")
            self.logger.info(f"Fallback client: {self.fallback_client_name}")
            
            if initialization_results:
                self.logger.info("Client initialization summary:")
                for client_name, success, error in initialization_results:
                    status = "✅ Success" if success else f"❌ Failed: {error}"
                    self.logger.info(f"  {client_name}: {status}")
            
            if not self.clients:
                # Actionable onboarding hint: the common cause is no provider API key
                # in the environment (the direct deepseek client is intentionally
                # disabled, so DEEPSEEK_API_KEY alone is not enough to bootstrap).
                # Reuse the single canonical no-key message (defined in the neutral
                # profiles module — modules/ never imports cli/).
                from modules.llm.profiles import no_key_message
                raise LLMError(no_key_message())
                
            self._initialized = True

        except Exception as e:
            self.logger.error(f"Failed to initialize LLM manager: {e}")
            raise LLMError(f"LLM manager initialization failed: {e}")

    async def _ensure_fallback_client(self) -> None:
        """Ensure an OpenAI fallback client (gpt-5) is available."""
        try:
            openai_config = self.llm_config.get('openai', {})
            
            # Check if OpenAI API key is available
            api_key = (
                openai_config.get('api_key') or 
                os.environ.get('OPENAI_API_KEY')
            )
            
            if not api_key:
                self.logger.debug("No OpenAI API key found - fallback client unavailable")
                return
            
            # Create fallback OpenAI client
            from modules.llm.openai_client import OpenAIClient
            fallback_client = OpenAIClient(self.config, name="openai_fallback_client")
            fallback_client.model_type = 'gpt-5'  # Always use gpt-5 for fallback
            fallback_client.api_key = api_key
            
            # FIXED: Configure fallback client with proper token limits from registry
            self._configure_client_token_limits(fallback_client, 'gpt-5')
            
            # Initialize the fallback client
            await fallback_client.initialize()
            
            # Register fallback client
            self.clients['openai_fallback_client'] = fallback_client
            if self.container:
                self.container.register_service('openai_fallback_client', fallback_client, is_optional=True)
            
            self.logger.info("✅ OpenAI fallback client initialized (gpt-5)")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize fallback client: {e}")

    async def _set_primary_client(self) -> None:
        """Set the primary client based on configuration or availability."""
        # Check for user-configured preference in chat_agent config
        preferred_client = self.config.get('chat_agent', {}).get('llm_client')
        if preferred_client and f"{preferred_client}_client" in self.clients:
            self.primary_client_name = f"{preferred_client}_client"
            self.logger.info(f"Using configured primary client: {self.primary_client_name}")
            return

        # Operator provider pin (CHAT_PROVIDER > DEFAULT_PROVIDER). The same pin is
        # already honored by ``core.runtime_config.resolve_runtime_config`` for goal
        # dispatch + chat — but WITHOUT this, a *dead-but-keyed* provider earlier in
        # the hardcoded ``priority_order`` below (e.g. an exhausted OpenRouter) is
        # always chosen primary and its 402 trips the provider-credit sentinel before
        # a funded pinned provider (e.g. ``zai-coding``) is ever used. Fail-open: a
        # pin whose client isn't registered falls through to the priority order.
        import os
        pinned_provider = os.environ.get('CHAT_PROVIDER') or os.environ.get('DEFAULT_PROVIDER')
        if pinned_provider:
            pinned_client = f"{pinned_provider}_client"
            if pinned_client in self.clients:
                self.primary_client_name = pinned_client
                self.logger.info(f"Using operator-pinned primary client: {self.primary_client_name}")
                return

        # Default priority order (prefer OpenAI as primary)
        priority_order = ['openai_client', 'anthropic_client', 'openrouter_client', 'gemini_client', 'deepseek_client']
        
        for client_name in priority_order:
            if client_name in self.clients:
                self.primary_client_name = client_name
                self.logger.info(f"Set primary client: {self.primary_client_name}")
                return
        
        # Use any available client
        if self.clients:
            self.primary_client_name = list(self.clients.keys())[0]
            self.logger.info(f"Using first available client as primary: {self.primary_client_name}")

    async def _set_fallback_client(self) -> None:
        """Set the fallback client."""
        # Always prefer the dedicated fallback client
        if 'openai_fallback_client' in self.clients:
            self.fallback_client_name = 'openai_fallback_client'
            return
            
        # Try the fallback hierarchy
        for client_name, _ in self.FALLBACK_HIERARCHY:
            if client_name in self.clients and client_name != self.primary_client_name:
                self.fallback_client_name = client_name
                return
        
        # Use any client except primary
        for client_name in self.clients:
            if client_name != self.primary_client_name:
                self.fallback_client_name = client_name
                return

    async def get_client(self, client_name: Optional[str] = None) -> Optional[LLMClient]:
        """Get LLM client by name or return primary client.
        
        IMPORTANT: If a specific client_name is requested but not available,
        this method returns None - it does NOT silently fallback to another client.
        Callers must handle None and implement their own fallback logic.
        
        Args:
            client_name: Specific client name to get, or None for primary/default
            
        Returns:
            LLM client instance or None if not found
        """
        if not self._initialized:
            await self.initialize()
        
        # Return specific client if requested
        if client_name:
            client = self.clients.get(client_name)
            if client:
                return client
            
            # Try to initialize the client if it doesn't exist
            provider = client_name.replace('_client', '')
            from modules.llm.model_registry import PROVIDER_CONFIG
            if provider in PROVIDER_CONFIG:
                try:
                    client = await self._try_initialize_client(provider)
                    if client:
                        return client
                except Exception as e:
                    self.logger.warning(f"Failed to initialize requested client {client_name}: {e}")
            
            # CRITICAL FIX: Do NOT silently fallback to a different client!
            # If caller asked for a specific client and it's not available, return None.
            # This prevents confusing behavior where anthropic is requested but openai is used.
            self.logger.warning(f"Requested client '{client_name}' is not available")
            return None
        
        # No specific client requested - return primary or any available
        if self.primary_client_name and self.primary_client_name in self.clients:
            return self.clients[self.primary_client_name]
        
        # Fallback to any available client
        if self.clients:
            client = list(self.clients.values())[0]
            self.logger.warning(f"No primary client available, using: {client.name}")
            return client
        
        return None

    async def get_client_with_fallback(self, preferred_client: Optional[str] = None) -> Optional[LLMClient]:
        """Get LLM client with automatic fallback on failure.
        
        Args:
            preferred_client: Preferred client name
            
        Returns:
            LLM client instance or None if all clients fail
        """
        if not self._fallback_enabled:
            return await self.get_client(preferred_client)
        
        # Try preferred client first
        if preferred_client:
            client = await self.get_client(preferred_client)
            if client and await self._test_client_health(client):
                return client
            self.logger.warning(f"Preferred client {preferred_client} unavailable or unhealthy")
        
        # Try primary client
        if self.primary_client_name:
            client = await self.get_client(self.primary_client_name)
            if client and await self._test_client_health(client):
                return client
            self.logger.warning(f"Primary client {self.primary_client_name} unavailable or unhealthy")
        
        # Try fallback hierarchy
        for client_name, model in self.FALLBACK_HIERARCHY:
            if client_name in self.clients:
                client = self.clients[client_name]
                if await self._test_client_health(client):
                    self.logger.info(f"Using fallback client: {client_name} with model {model}")
                    return client
        
        # Last resort - try any available client
        for client_name, client in self.clients.items():
            if await self._test_client_health(client):
                self.logger.warning(f"Using last resort client: {client_name}")
                return client
        
        self.logger.error("All LLM clients are unavailable or unhealthy")
        return None

    async def _test_client_health(self, client: LLMClient) -> bool:
        """Test if a client is healthy and responsive.
        
        Args:
            client: LLM client to test
            
        Returns:
            True if client is healthy, False otherwise
        """
        try:
            if not getattr(client, '_initialized', False):
                return False
            
            # Basic test - try to validate config
            if hasattr(client, '_validate_llm_config'):
                client._validate_llm_config()
            
            return True
            
        except Exception as e:
            self.logger.debug(f"Client health check failed for {client.name}: {e}")
            return False

    async def _try_initialize_client(self, provider: str) -> Optional[LLMClient]:
        """Try to initialize a client for the given provider.
        
        Args:
            provider: Provider name (anthropic, openai, etc.)
            
        Returns:
            Initialized client or None if failed
        """
        # Prevent multiple initialization attempts
        if provider in self._initialization_attempts:
            self.logger.debug(f"Client initialization already attempted for {provider}")
            return None
        
        self._initialization_attempts[provider] = True
        
        try:
            config_data = self.llm_config.get(provider, {})
            if not config_data.get('api_key'):
                return None
            
            model_type = config_data.get('model') or get_default_model(provider)
            if not model_type:
                return None
            
            # Create and initialize client
            client = create_llm_client(
                name=provider,
                config=self.config,
                container=self.container,
                model_type=model_type
            )
            
            # FIXED: Configure client with intelligent token limits from registry
            self._configure_client_token_limits(client, model_type)
            
            await client.initialize()
            
            # Register client
            service_name = f"{provider}_client"
            self.clients[service_name] = client
            
            if self.container:
                self.container.register_service(service_name, client, is_optional=True)
            
            self.logger.info(f"Successfully initialized {provider} client on demand")
            return client
            
        except Exception as e:
            self.logger.error(f"Failed to initialize {provider} client on demand: {e}")
            return None

    async def _create_isolated_client(self, provider: str, model: str) -> Optional[LLMClient]:
        """Build a FRESH, non-cached client for an isolated model (e.g. compaction aux).

        Mirrors :meth:`_try_initialize_client` but deliberately does NOT register the
        client in ``self.clients`` / the container. Adapters mutate the wrapped client's
        ``model_type``; using an isolated instance means a same-provider aux model can't
        clobber the shared per-provider client the main agent uses. Returns None when the
        provider has no API key (caller falls back to the main model).
        """
        if not self._initialized:
            await self.initialize()

        config_data = self.llm_config.get(provider, {})
        if not config_data.get('api_key'):
            self.logger.debug(f"No API key for isolated {provider} client; skipping aux")
            return None

        try:
            client = create_llm_client(
                name=provider,
                config=self.config,
                container=self.container,
                model_type=model,
            )
            self._configure_client_token_limits(client, model)
            await client.initialize()
            self.logger.info(f"Isolated aux client built (not cached): {provider}/{model}")
            return client
        except Exception as e:
            self.logger.warning(f"Could not build isolated {provider} client for {model}: {e}")
            return None

    async def _cleanup(self) -> None:
        """Clean up LLM Manager resources."""
        try:
            # Clean up all clients
            for name, client in self.clients.items():
                try:
                    await client.cleanup()
                    self.logger.info(f"✓ {name} cleaned up")
                except Exception as e:
                    self.logger.error(f"Error cleaning up {name}: {e}")
            
            self.clients.clear()
            self.primary_client_name = None
            self.logger.info("LLM Manager cleaned up successfully")
            
        except Exception as e:
            self.logger.error(f"Error during LLM Manager cleanup: {e}")
            raise

    async def get_available_models(self, provider: Optional[str] = None) -> List[Tuple[str, str]]:
        """Get a flat list of available models with their providers.
        
        IMPORTANT: Only returns models from providers that have successfully
        initialized clients. This prevents the UI from showing unavailable options.
        
        Args:
            provider: Optional provider name to filter results
            
        Returns:
            List of (provider, model_name) tuples for INITIALIZED providers only
        """
        if not self._initialized:
            await self.initialize()

        # Get list of initialized providers (clients that successfully initialized)
        initialized_providers = set()
        for client_name in self.clients.keys():
            # Extract provider name from client name (e.g., "openai_client" -> "openai")
            prov = client_name.replace('_client', '').replace('_fallback', '')
            initialized_providers.add(prov)

        if provider:
            # Only return if this specific provider is initialized
            if provider not in initialized_providers and f"{provider}_client" not in self.clients:
                self.logger.debug(f"Provider '{provider}' requested but not initialized")
                return []
            wanted = {provider}
        else:
            wanted = initialized_providers

        # P0.6: delegate the model list to the ONE catalog (modules.llm.available_models)
        # instead of reading AVAILABLE_MODELS directly. Same (provider, model) tuples for the
        # same initialized providers (an initialized provider always has a usable key, so
        # `usable ∩ wanted == wanted`); the registry model set is identical (non-deprecated).
        from modules.llm.available_models import available_models as _build_models
        choices = _build_models(initialized_only=True, initialized_providers=wanted)
        return [(c.provider, c.model) for c in choices]

    async def get_available_clients(self) -> Dict[str, Dict[str, Any]]:
        """Get comprehensive information about all available LLM clients.
        
        Returns:
            Dictionary mapping client names to their metadata including provider, model,
            initialization status, and available models for each client
        """
        if not self._initialized:
            await self.initialize()
            
        result = {}
        
        # First add clients that are already initialized
        for name, client in self.clients.items():
            provider = name.replace('_client', '')
            is_primary = (name == self.primary_client_name)
            
            # FIXED: Get token limits from model registry if available
            model_config = None
            max_tokens = getattr(client, 'max_tokens', None)
            if max_tokens is None:
                # Try to get intelligent default from model registry
                if hasattr(client, 'model_type') and client.model_type:
                    model_config = get_model_config(client.model_type)
                    if model_config and model_config.max_completion_tokens:
                        max_tokens = model_config.max_completion_tokens
                
                # Fallback to conservative default instead of 1000
                if max_tokens is None:
                    max_tokens = 8000
            
            # Build comprehensive metadata
            result[name] = {
                'name': name,
                'provider': provider,
                'model': client.model_type,
                'initialized': getattr(client, '_initialized', False),
                'is_primary': is_primary,
                'max_tokens': max_tokens,
                'temperature': getattr(client, 'temperature', 0.7),
                'available_models': AVAILABLE_MODELS.get(provider, []),
                'context_window': model_config.context_window if model_config else None,
                'pricing': {
                    'input_price': model_config.pricing.input_price if model_config else None,
                    'output_price': model_config.pricing.output_price if model_config else None
                } if model_config else None
            }
        
        # Also include clients that aren't initialized but have config
        for provider in AVAILABLE_MODELS.keys():
            client_name = f"{provider}_client"
            if client_name not in result:
                # Check if we have config for this provider
                config_data = self.llm_config.get(provider, {})
                if config_data and 'api_key' in config_data:
                    model_name = config_data.get('model') or get_default_model(provider)
                    model_config = get_model_config(model_name) if model_name else None
                    
                    result[client_name] = {
                        'name': client_name,
                        'provider': provider,
                        'model': model_name,
                        'initialized': False,
                        'is_primary': False,
                        'max_tokens': model_config.max_completion_tokens if model_config else 8000,
                        'available_models': AVAILABLE_MODELS.get(provider, []),
                        'context_window': model_config.context_window if model_config else None,
                        'pricing': {
                            'input_price': model_config.pricing.input_price if model_config else None,
                            'output_price': model_config.pricing.output_price if model_config else None
                        } if model_config else None
                    }
        
        return result

    async def get_chat_model(self,
                               provider: str,
                               model: str,
                               temperature: float = 0.0,
                               isolated_client: bool = False,
                               **kwargs) -> BaseChatModel:
        """Get a native chat model for the requested provider.

        This method gets the appropriate LLM client and creates a native chat model
        based on the provider and model type.

        Args:
            provider: Provider name (openai, anthropic, llama, deepseek, gemini)
            model: Model name
            temperature: Temperature for generation
            **kwargs: Additional parameters to pass to the model

        Returns:
            Native chat model

        Raises:
            ValueError: If no client is available for the provider
        """
        if not self._initialized:
            await self.initialize()

        # Try to get the client for the provider.
        # isolated_client=True builds a FRESH, non-cached client (e.g. compaction aux)
        # so its model_type can't clobber the shared per-provider client (see
        # _create_isolated_client). On failure we raise so the aux caller falls back to
        # the main model rather than silently mutating the shared client.
        client_name = f"{provider}_client"
        if isolated_client:
            llm_client = await self._create_isolated_client(provider, model)
            if not llm_client:
                raise ValueError(f"Could not build isolated client for provider '{provider}'")
        else:
            llm_client = await self.get_client(client_name)
            # Concurrency fix: if the requested model differs from the shared
            # per-provider client's model, build an isolated client rather than let
            # create_chat_model mutate the shared client's model_type/capabilities in
            # place — that mutation bleeds across concurrent sessions using the same
            # provider with different models (openrouter/nvidia/gemini). Same rationale
            # as the isolated_client=True path above. Fail-open: if isolation can't be
            # built, fall back to the shared client (legacy mutate behavior).
            if llm_client is not None and getattr(llm_client, "model_type", model) != model:
                try:
                    isolated = await self._create_isolated_client(provider, model)
                    if isolated is not None:
                        llm_client = isolated
                except Exception as e:
                    self.logger.debug(f"per-model isolated client fell back to shared: {e}")

        if not llm_client:
            # FIXED: Don't fall back to different provider - fail with clear error
            # Silently changing providers is confusing and unexpected
            self.logger.error(f"No client found for provider {provider}")

            # List available providers for helpful error message
            available = [name.replace('_client', '') for name in self.clients.keys()]

            raise ValueError(self._provider_unavailable_message(provider, available))

        # CRITICAL: Verify we got the RIGHT client type, not a fallback
        # get_client() can return a fallback client if the requested one isn't available
        # This causes confusing errors where GeminiAdapter gets an OpenAIClient
        # 024 fix: a spec-backed generic client (OpenAICompatClient /
        # AnthropicCompatClient) carries its provider on `_spec.name` — the
        # class-name substring test below can NEVER match it ("ollama" is not in
        # "openaicompatclient"), so check the spec identity first.
        client_spec = getattr(llm_client, "_spec", None)
        spec_matches = client_spec is not None and getattr(client_spec, "name", None) == provider
        client_type_name = type(llm_client).__name__.lower()
        if not spec_matches and provider not in client_type_name:
            self.logger.error(f"Client type mismatch: requested {provider} but got {type(llm_client).__name__}")
            available = [name.replace('_client', '') for name in self.clients.keys()]
            raise ValueError(
                f"LLM client type mismatch for provider '{provider}': got {type(llm_client).__name__} instead. "
                f"Available providers: {', '.join(available) if available else 'none'}. "
                f"The requested provider's client failed to initialize. "
                f"Check your API keys and initialization logs for '{provider}' provider."
            )

        # Import here to avoid circular imports
        from modules.llm.llm_factory import create_chat_model

        # FIXED: Get intelligent max_tokens from model registry
        max_tokens = kwargs.pop('max_tokens', None)
        if max_tokens is None:
            model_config = get_model_config(model)
            if model_config and model_config.max_completion_tokens:
                max_tokens = model_config.max_completion_tokens
                kwargs['max_tokens'] = max_tokens
                self.logger.debug(f"Using max_tokens={max_tokens} from model registry for {model}")

        # Create and return the appropriate chat model
        # Provider is preserved - no silent fallback to OpenAI
        return create_chat_model(
            provider=provider,
            model=model,
            temperature=temperature,
            llm_client=llm_client,
            **kwargs
        )

    async def get_fallback_chat_model(
        self,
        exclude_providers: Optional[List[str]] = None,
        original_model: Optional[str] = None,
        temperature: float = 0.0,
        **kwargs
    ) -> Optional[BaseChatModel]:
        """Get a fallback chat model after primary provider failure.
        
        This method is used by the task agent when the primary LLM provider fails
        (rate limit, authentication error, etc.) to automatically switch to the
        next available provider in the fallback hierarchy.
        
        Args:
            exclude_providers: List of providers to skip (e.g., ones that already failed)
            original_model: Original model name for logging context
            temperature: Temperature for generation (default 0.0)
            **kwargs: Additional parameters to pass to the chat model
            
        Returns:
            Chat model from the next available provider, or None if all failed
            
        Example:
            ```python
            # In task agent after catching LLMRateLimitError
            fallback_llm = await llm_manager.get_fallback_chat_model(
                exclude_providers=['openai'],
                original_model='gpt-5'
            )
            if fallback_llm:
                response = await fallback_llm.ainvoke(messages)
            ```
        """
        from modules.llm.llm_factory import create_chat_model
        
        if not self._initialized:
            await self.initialize()
        
        exclude_providers = exclude_providers or []
        
        self.logger.info(
            f"🔄 Searching for fallback provider (excluding: {exclude_providers}, "
            f"original: {original_model or 'unknown'})"
        )
        
        # Operator-pinned primary first: the deployment's actual serving client
        # (e.g. a funded subscription seat with fallback_eligible=False, absent
        # from FALLBACK_HIERARCHY) beats the generic hierarchy. Exclusions still
        # apply below, so the provider that just failed is never retried here.
        candidates = list(self.FALLBACK_HIERARCHY)
        primary = self.primary_client_name
        if primary:
            primary_model = getattr(self.clients.get(primary), 'model_type', None)
            if not primary_model:
                try:
                    from modules.llm.llm_client_registry import get_default_model
                    primary_model = get_default_model(_provider_of(primary))
                except Exception:
                    primary_model = None
            if primary_model:
                candidates = [(primary, primary_model)] + [
                    c for c in candidates if c[0] != primary
                ]

        # Try the primary, then each provider in the fallback hierarchy
        for client_name, fallback_model in candidates:
            provider = _provider_of(client_name)
            
            # Skip excluded providers
            if provider in exclude_providers or client_name in exclude_providers:
                self.logger.debug(f"Skipping excluded provider: {provider}")
                continue
            
            # Check if client is available
            if client_name not in self.clients:
                self.logger.debug(f"Provider {provider} not initialized, trying to initialize...")
                # Try to initialize on-demand
                client = await self._try_initialize_client(provider)
                if not client:
                    self.logger.debug(f"Could not initialize {provider}, skipping")
                    continue
            else:
                client = self.clients[client_name]
            
            # Test client health
            if not await self._test_client_health(client):
                self.logger.debug(f"Provider {provider} health check failed, skipping")
                continue
            
            # Found a healthy provider - create chat model
            self.logger.info(
                f"✅ Found fallback provider: {provider} with model {fallback_model} "
                f"(original was: {original_model or 'unknown'})"
            )
            
            try:
                # Get intelligent max_tokens from model registry
                max_tokens = kwargs.pop('max_tokens', None)
                if max_tokens is None:
                    fallback_config = get_model_config(fallback_model)
                    if fallback_config and fallback_config.max_completion_tokens:
                        max_tokens = fallback_config.max_completion_tokens
                        kwargs['max_tokens'] = max_tokens

                # Build on an ISOLATED client so a same-provider failover can't clobber
                # the main agent's live client. Adapters mutate model_type in place; the
                # shared cached `client` above is fine for the health check, but the model
                # itself must run on a fresh, non-cached client.
                iso_client = await self._create_isolated_client(provider, fallback_model)
                if iso_client is None:
                    self.logger.debug(
                        f"Could not build isolated {provider} client for {fallback_model}, skipping"
                    )
                    continue

                return create_chat_model(
                    provider=provider,
                    model=fallback_model,
                    temperature=temperature,
                    llm_client=iso_client,
                    **kwargs
                )
            except Exception as e:
                self.logger.warning(f"Failed to create chat model for {provider}: {e}")
                # Add this provider to exclusions and continue
                exclude_providers.append(provider)
                continue
        
        # No fallback available
        self.logger.error(
            f"❌ No fallback providers available. Tried hierarchy, all failed or excluded. "
            f"Excluded: {exclude_providers}"
        )
        return None
