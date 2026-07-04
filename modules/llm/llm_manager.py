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
from modules.llm.anthropic_client import AnthropicClient
from modules.llm.openai_client import OpenAIClient
from modules.llm.gemini_client import GeminiClient
from modules.llm.openrouter_client import OpenRouterClient

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


class LLMManager(BaseComponent):
    """Service for managing LLM clients and configurations."""

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
        
        # Hardcoded fallback hierarchy (Dec 2025)
        # NOTE: deepseek_client DISABLED - use OpenRouter's DeepSeek instead
        self.FALLBACK_HIERARCHY = [
            ('openai_client', 'gpt-5'),  # Ultimate fallback - GPT-5
            ('anthropic_client', 'claude-sonnet-4-5'),
            ('openrouter_client', 'z-ai/glm-5.2'),  # OpenRouter default = Z.AI GLM flagship (was grok-4.3)
            ('gemini_client', 'gemini-2.5-flash'),
        ]

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
                                self.logger.info(f"Using existing {client_name.title()} LLM client from container")
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
                        self.logger.info(f"✅ {client_name.title()} LLM client initialized with model {model_type}")
                        
                    except Exception as e:
                        initialization_results.append((client_name, False, str(e)))
                        self.logger.warning(f"❌ Failed to initialize {client_name} client: {e}")
                        continue
                else:
                    self.logger.debug(f"⚠️ {client_name.title()} LLM client not configured (missing config or API key)")

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
        """Ensure OpenAI GPT-4.1 client is available as fallback."""
        try:
            openai_config = self.llm_config.get('openai', {})
            
            # Check if OpenAI API key is available
            api_key = (
                openai_config.get('api_key') or 
                os.environ.get('OPENAI_API_KEY')
            )
            
            if not api_key:
                self.logger.warning("No OpenAI API key found - fallback client unavailable")
                return
            
            # Create fallback OpenAI client with GPT-4.1
            fallback_client = OpenAIClient(self.config, name="openai_fallback_client")
            fallback_client.model_type = 'gpt-5'  # Always use GPT-4.1 for fallback
            fallback_client.api_key = api_key
            
            # FIXED: Configure fallback client with proper token limits from registry
            self._configure_client_token_limits(fallback_client, 'gpt-5')
            
            # Initialize the fallback client
            await fallback_client.initialize()
            
            # Register fallback client
            self.clients['openai_fallback_client'] = fallback_client
            if self.container:
                self.container.register_service('openai_fallback_client', fallback_client, is_optional=True)
            
            self.logger.info("✅ OpenAI GPT-4.1 fallback client initialized")
            
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
            if provider in ['anthropic', 'openai', 'deepseek', 'gemini', 'openrouter', 'nvidia']:
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

    async def get_client_for_provider(self, provider: str) -> Optional[LLMClient]:
        """Get a client instance for a specific provider, initializing on demand if needed.

        Args:
            provider: Provider name (e.g., 'openai', 'anthropic')

        Returns:
            LLMClient or None if unavailable

        Note:
            This method does NOT fall back to OpenAI if the requested provider isn't available.
            Use get_client_with_fallback() if you want automatic fallback behavior.
        """
        if not self._initialized:
            await self.initialize()

        # Exact client if already available
        client_name = f"{provider}_client"
        if client_name in self.clients:
            return self.clients[client_name]

        # Try to initialize on demand
        client = await self._try_initialize_client(provider)
        if client:
            return client

        # FIXED: Don't fall back to OpenAI - return None if provider unavailable
        # Callers should check for None and handle appropriately
        self.logger.warning(f"Client for provider '{provider}' is not available")
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

    async def set_primary_client(self, client_name: str) -> bool:
        """Set the primary LLM client.
        
        Args:
            client_name: Name of the client to set as primary
            
        Returns:
            True if successful, False otherwise
        """
        if not self._initialized:
            await self.initialize()
        
        if client_name not in self.clients:
            # Try to initialize the client
            provider = client_name.replace('_client', '')
            client = await self._try_initialize_client(provider)
            if not client:
                self.logger.error(f"Cannot set primary client - {client_name} not available")
                return False
        
        self.primary_client_name = client_name
        self.logger.info(f"Primary client set to: {client_name}")
        return True

    async def enable_fallback(self, enabled: bool = True) -> None:
        """Enable or disable fallback mechanism.
        
        Args:
            enabled: Whether to enable fallback
        """
        self._fallback_enabled = enabled
        self.logger.info(f"Fallback mechanism {'enabled' if enabled else 'disabled'}")

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

    async def get_primary_client(self) -> Optional[LLMClient]:
        """Get the primary LLM client."""
        if not self._initialized:
            await self.initialize()
        if not self.primary_client_name:
            return None
        return self.clients.get(self.primary_client_name)

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

    async def update_client_settings(self, client_name: str, settings: Dict[str, Any]) -> bool:
        """Update settings for a specific LLM client."""
        if not self._initialized:
            await self.initialize()
            
        client = self.clients.get(client_name)
        if not client:
            self.logger.warning(f"Client '{client_name}' not found for settings update")
            return False
        
        try:
            # Use the new update_settings method if available
            if hasattr(client, 'update_settings'):
                client.update_settings(settings)
                self.logger.info(f"Updated {client_name} settings using update_settings() method")
                
                # FIXED: Reconfigure token limits if model was changed
                if 'model' in settings or 'model_type' in settings:
                    new_model = settings.get('model') or settings.get('model_type')
                    if new_model:
                        self._configure_client_token_limits(client, new_model)
                        self.logger.info(f"Reconfigured token limits for {client_name} with new model {new_model}")
                
                return True
                
            # Legacy fallback - update settings directly
            for key, value in settings.items():
                if key == 'model_type':
                    # Special handling for model type - requires token limit reconfiguration
                    if hasattr(client, 'model_type'):
                        prev_model = getattr(client, 'model_type', 'unknown')
                        setattr(client, 'model_type', value)
                        
                        # Reconfigure token limits for new model
                        self._configure_client_token_limits(client, value)
                        self.logger.info(f"Updated {client_name} model_type: {prev_model} → {value}")
                        
                elif hasattr(client, key):
                    # Log the previous value for debugging
                    prev_val = getattr(client, key)
                    setattr(client, key, value)
                    self.logger.info(f"Updated {client_name} setting {key}={value} (was {prev_val})")
                else:
                    self.logger.warning(f"Client {client_name} doesn't have attribute {key}, skipping")
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to update {client_name} settings: {e}")
            return False

    async def validate_client(self, client_name: str) -> Tuple[bool, Optional[str]]:
        """Validate that a client is working correctly."""
        if not self._initialized:
            await self.initialize()
            
        client = self.clients.get(client_name)
        if not client:
            return False, "Client not found"
        
        try:
            if hasattr(client, 'validate'):
                # Some clients have a dedicated validation method
                await client.validate()
            else:
                # Otherwise, try a simple generation
                await client.generate_response(
                    prompt="Hello",
                    max_tokens=5
                )
                
            # Special checking for DeepSeek client
            if 'deepseek' in client_name.lower():
                self.logger.info(f"DeepSeek client {client.model_type} validated successfully")
                
            return True, None
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Validation failed for {client_name}: {error_msg}")
            return False, error_msg

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

            raise ValueError(
                f"LLM client for provider '{provider}' is not available. "
                f"Available providers: {', '.join(available) if available else 'none'}. "
                f"Check your API keys in config/.env.{os.environ.get('ENV', 'development')}"
            )

        # CRITICAL: Verify we got the RIGHT client type, not a fallback
        # get_client() can return a fallback client if the requested one isn't available
        # This causes confusing errors where GeminiAdapter gets an OpenAIClient
        client_type_name = type(llm_client).__name__.lower()
        if provider not in client_type_name:
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
        
        # Try each provider in the fallback hierarchy
        for client_name, fallback_model in self.FALLBACK_HIERARCHY:
            provider = client_name.replace('_client', '')
            
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

    def get_provider_from_model(self, model_name: str) -> str:
        """Extract provider name from model name using model registry.
        
        REFACTORED (Dec 2025): Uses model_registry as single source of truth.
        Removed redundant fallback pattern matching - model_registry handles
        unknown models with its own fallback chain.
        
        Args:
            model_name: Model name (e.g., 'gpt-5', 'claude-sonnet-4-5')
            
        Returns:
            Provider name (e.g., 'openai', 'anthropic', 'gemini', 'deepseek')
        """
        if not model_name:
            return 'unknown'
        
        # Use model registry as SINGLE SOURCE OF TRUTH
        # model_registry.get_model() already handles unknown models with fallback chain
        model_config = get_model_config(model_name)
        if model_config and model_config.provider:
            # M1 FIX: route through the single source of truth (includes NVIDIA + CUSTOM)
            # instead of a hand-rolled map that drifts — the old map omitted NVIDIA, so
            # Kimi/NVIDIA models returned 'unknown' (which also defeated billing failover's
            # provider-exclusion bookkeeping).
            from modules.llm.model_registry import canonical_provider_name
            return canonical_provider_name(model_config.provider, default='unknown')

        return 'unknown'