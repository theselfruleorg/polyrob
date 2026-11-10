"""LLM Manager for centralizing LLM client management."""

import logging
import asyncio
import math
from typing import Dict, Any, FrozenSet, Optional, List, Tuple, Set, Union

# Native BaseChatModel
from modules.llm.adapters import BaseChatModel

from core.base_component import BaseComponent
from core.config import BotConfig
from core.container import DependencyContainer
from core.exceptions import LLMConfigError, LLMError, ServiceError

from modules.llm.llm_client import LLMClient
from modules.llm.init_gate import (
    ProviderInitGate,
    provider_of_client_name as _provider_of,
    recent_call_error,
)
from modules.llm.manager_inventory import InventoryMixin

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

from core.price_guard import GUARD_RATIO, guard_reason

from modules.llm.catalog_lookup import catalog_wire_id
from modules.llm.model_registry import get_model_config, get_model_config_exact
from modules.llm.model_types import model_family


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


def _model_ids(model: Optional[str]) -> FrozenSet[str]:
    """Every name of one model with the route's vendor slug removed, so a provider's
    own id and the OpenRouter slug of the same model share a name."""
    config = get_model_config_exact(model) if model else None
    names = [model, *(([config.name, *config.aliases]) if config else [])]
    return frozenset(n.rsplit("/", 1)[-1] for n in names if n)


def _known_price(provider: Optional[str], model: Optional[str]) -> Optional[Tuple[float, float]]:
    """``(input, output)`` USD per million tokens, or None when the price is unknown:
    no exact row, or a variable (negative) price. A flat-rate seat costs nothing per token."""
    from modules.llm.provider_spec import is_flat_rate
    if provider and is_flat_rate(provider):
        return (0.0, 0.0)
    pricing = getattr(get_model_config_exact(model) if model else None, "pricing", None)
    try:
        rates = (float(pricing.input_price), float(pricing.output_price))
    except (AttributeError, TypeError, ValueError):
        return None
    return rates if all(math.isfinite(r) and r >= 0 for r in rates) else None


def _price_refusal(provider: str, model: str, original_model: Optional[str]) -> str:
    """Why a fallback rung may not silently replace *original_model*: the rule of the
    ``/model`` price guard, and the rung's own unknown price refuses (``""`` = allowed).
    With no known current price, the guard reads the rung's own price only, so a model
    with no catalog row can still fail over."""
    if not original_model:
        return ""
    new = _known_price(provider, model)
    if new is None:
        return "its price is unknown"
    return guard_reason(_known_price(None, original_model), new)


def _money_refusal(model: str, original_model: Optional[str]) -> str:
    """A money run stays on its own model (another route) or on ``HEAVY_MODEL``."""
    from core.runtime_config import heavy_model_pin
    if _model_ids(model) & (_model_ids(original_model) | _model_ids(heavy_model_pin())):
        return ""
    return "a money run stays on a money-class model"


class LLMManager(InventoryMixin, BaseComponent):
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
        self.llm_config = config.get_llm_config()
        # Per-provider init state with backoff (never a permanent latch),
        # and the per-provider health that real calls stamp through ``note_call_error``.
        self._init_gate = ProviderInitGate()
        
        # Fallback hierarchy — derived from the ProviderSpec registry.
        self.FALLBACK_HIERARCHY = self._build_fallback_hierarchy()

    @staticmethod
    def _build_fallback_hierarchy():
        """(client_name, model) fallback pairs.

        Derived: fallback-eligible specs ordered by their ``fallback_rank``, models
        from the DEFAULT_MODELS policy table (or the spec's own default for a
        providers.yaml row that opted into ``fallback_eligible: true``, appended after
        the built-ins).
        """
        from modules.llm.provider_spec import BUILTIN_SPECS, get_specs
        try:
            specs = get_specs()
        except Exception:
            specs = BUILTIN_SPECS  # a broken providers.yaml must not blank the hierarchy
        eligible = [s for s in specs if s.fallback_eligible]
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
        return out

    def _configure_client_token_limits(self, client: LLMClient, model_name: str) -> None:
        """Configure client with appropriate token limits from model registry.
        
        Delegates to client._configure_from_model_registry()
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
            
            # Also set on underlying LLM if it exists (manager-specific)
            if hasattr(client, 'llm') and hasattr(client.llm, 'max_tokens'):
                client.llm.max_tokens = get_model_config(model_name).max_completion_tokens

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
            from modules.llm.profiles import PROFILES
            clients_to_try = [p.name for p in PROFILES.values() if p.initializable]
            initialized_clients = []
            initialization_results = []
            
            # Log a REDACTED view — never the raw config (it holds live api_keys,
            # and this fires on every container build, incl. on the CLI's stdout).
            self.logger.info(f"LLM config (providers): {_redact_llm_config(self.llm_config)}")
            
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
                                existing_client._health_gate = self._init_gate
                                
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
                        client._health_gate = self._init_gate

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

            # Log summary
            self.logger.info(f"LLM Manager initialized with {len(self.clients)} clients")
            self.logger.info(f"Primary client: {self.primary_client_name}")
            
            if initialization_results:
                self.logger.info("Client initialization summary:")
                for client_name, success, error in initialization_results:
                    status = "✅ Success" if success else f"❌ Failed: {error}"
                    self.logger.info(f"  {client_name}: {status}")
            
            if not self.clients:
                # Actionable onboarding hint: the common cause is no provider API key
                # in the environment.
                # Reuse the single canonical no-key message (defined in the neutral
                # profiles module — modules/ never imports cli/).
                from modules.llm.profiles import no_key_message
                raise LLMConfigError(no_key_message())
                
            self._initialized = True

        except LLMError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize LLM manager: {e}")
            raise LLMConfigError(f"LLM manager initialization failed: {e}") from e

    async def _set_primary_client(self) -> None:
        """Set the primary client based on configuration or availability."""
        # Check for user-configured preference in chat_agent config
        preferred_client = self.config.get('chat_agent', {}).get('llm_client')
        if preferred_client and f"{preferred_client}_client" in self.clients:
            self.primary_client_name = f"{preferred_client}_client"
            self.logger.info(f"Using configured primary client: {self.primary_client_name}")
            return

        # Operator provider pin (DEFAULT_PROVIDER). The same pin is
        # already honored by ``core.runtime_config.resolve_runtime_config`` for goal
        # dispatch + chat — but WITHOUT this, a *dead-but-keyed* provider earlier in
        # the hardcoded ``priority_order`` below (e.g. an exhausted OpenRouter) is
        # always chosen primary and its 402 trips the provider-credit sentinel before
        # a funded pinned provider (e.g. ``zai-coding``) is ever used. Fail-open: a
        # pin whose client isn't registered falls through; an alias (`glm`) → `zai_client`.
        from core.runtime_config import canonicalize_provider, operator_provider_pin
        pinned_provider = canonicalize_provider(operator_provider_pin())
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
            
            # CRITICAL: Do NOT silently fallback to a different client!
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
            if recent_call_error(client):  # a real call just failed
                return False
            
            # Basic test - try to validate config
            if hasattr(client, '_validate_llm_config'):
                client._validate_llm_config()
            
            return True
            
        except Exception as e:
            self.logger.debug(f"Client health check failed for {client.name}: {e}")
            return False

    async def _try_initialize_client(self, provider: str) -> Optional[LLMClient]:
        """Initialize a provider's client on demand (None if not possible now).
        One attempt at a time per provider, a failure retries after
        a backoff; a key set after start-up is read on the next attempt."""
        return await self._init_gate.acquire(
            provider, lambda: self.clients.get(f"{provider}_client"),
            lambda: self._initialize_client_once(provider))

    async def _initialize_client_once(self, provider: str) -> Optional[LLMClient]:
        try:
            config_data = self.llm_config.get(provider, {})
            if not config_data.get('api_key'):
                config_data = (self.config.get_llm_config() or {}).get(provider, {})
                if not config_data.get('api_key'):
                    return None
                self.llm_config[provider] = config_data
            
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
            client._health_gate = self._init_gate

            # Configure client with intelligent token limits from registry
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
        client in ``self.clients`` / the container, and makes no validation call. Adapters
        mutate the wrapped client's ``model_type``; using an isolated instance means a
        same-provider aux model can't clobber the shared per-provider client the main agent
        uses. The session that asked for it closes it. Returns None when the provider has
        no API key (caller falls back to the main model).
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
                isolated=True,
            )
            client._health_gate = self._init_gate
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
            self._init_gate.reset()
            self.logger.info("LLM Manager cleaned up successfully")
            
        except Exception as e:
            self.logger.error(f"Error during LLM Manager cleanup: {e}")
            raise

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
            provider: Provider name (a ``ProviderSpec`` name)
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
            # provider with different models. Refuse when isolation fails:
            # the shared client can belong to another tenant's active request.
            wire_ids = getattr(llm_client, "WIRE_ID_PROVIDER", None)
            if llm_client is not None and (
                    catalog_wire_id(getattr(llm_client, "model_type", model), wire_ids)
                    != catalog_wire_id(model, wire_ids)):
                try:
                    isolated = await self._create_isolated_client(provider, model)
                    if isolated is None:
                        raise ValueError("Could not isolate the requested model")
                    llm_client = isolated
                except Exception as e:
                    raise ValueError("Could not isolate the requested model; shared client unchanged") from e

        if not llm_client:
            # Don't fall back to a different provider - fail with clear error
            # Silently changing providers is confusing and unexpected
            self.logger.error(f"No client found for provider {provider}")

            # List available providers for helpful error message
            available = [name.replace('_client', '') for name in self.clients.keys()]

            raise ValueError(self._provider_unavailable_message(provider, available))

        # CRITICAL: Verify we got the RIGHT client type, not a fallback
        # get_client() can return a fallback client if the requested one isn't available
        # This causes confusing errors where GeminiAdapter gets an OpenAIClient
        # A spec-backed generic client (OpenAICompatClient /
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

        # A missing SDK polls time.sleep up to 120 s; the factory call is then a no-op.
        from core.lazy_deps import LAZY_FEATURES, ensure_provider
        if f"provider.{provider}" in LAZY_FEATURES:
            await asyncio.to_thread(ensure_provider, f"provider.{provider}")

        # Import here to avoid circular imports
        from modules.llm.llm_factory import create_chat_model

        # The factory resolves the per-request max_tokens. Provider is preserved: no silent fallback.
        return create_chat_model(
            provider=provider,
            model=model,
            temperature=temperature,
            llm_client=llm_client,
            **kwargs
        )

    def provider_ready(self, provider: str) -> bool:
        """False while *provider* is credit-dead or a real call just failed on it."""
        return not (self._credit_dead(provider) or self._init_gate.recent_call_error(provider))

    @staticmethod
    def _credit_dead(provider: str) -> bool:
        """The credit sentinel has latched *provider*. A keyed but unfunded provider
        passes the health check, and only its real call gets the 402. Fail-open: a
        sentinel probe error never blocks a candidate."""
        try:
            from core.credit_sentinel import credit_sentinel_active
            return bool(credit_sentinel_active(provider))
        except Exception:
            return False

    def _provider_rungs(self) -> List[Tuple[str, str]]:
        """``(client_name, model)`` of each provider: the primary first (it may be a seat
        outside the hierarchy), and the pinned provider serves the operator's model."""
        from core.runtime_config import operator_model_for
        rungs = [(name, operator_model_for(_provider_of(name)) or model)
                 for name, model in self.FALLBACK_HIERARCHY]
        primary = self.primary_client_name
        if primary:
            primary_model = (getattr(self.clients.get(primary), 'model_type', None)
                             or get_default_model(_provider_of(primary)))
            rungs = [(primary, primary_model)] + [r for r in rungs if r[0] != primary]
        return rungs

    @staticmethod
    def _family_model(provider: str, default: str, original_model: str,
                      family: str) -> Optional[str]:
        """The model of *family* on *provider* nearest to the failed model, or None: the
        same model on this route first, else the nearest input price inside the guard's
        ratio, so a rung keeps the tier. A far cheaper row of the family is not this model."""
        ids = _model_ids(original_model)
        price = _known_price(None, original_model)

        def gap(model: str) -> float:
            other = _known_price(None, model)
            return (abs(math.log(other[0] / price[0]))
                    if other and price and other[0] > 0 and price[0] > 0 else math.inf)

        rows = [(not _model_ids(m) & ids, gap(m), m)
                for m in dict.fromkeys([default, *AVAILABLE_MODELS.get(provider, [])])
                if model_family(m) == family]
        fitting = [r for r in rows if not r[0] or r[1] <= math.log(GUARD_RATIO)]
        return min(fitting, key=lambda r: r[:2])[2] if fitting else None

    def _fallback_ladder(self, original_model: Optional[str]) -> List[Tuple[str, str]]:
        """The ONE ladder: the failed model's family on each provider, then each
        provider's own model. Same-model retries with backoff live in the agent."""
        rungs = self._provider_rungs()
        family = model_family(original_model)
        if not family:
            return rungs
        same_family = []
        for name, default in rungs:
            model = self._family_model(_provider_of(name), default, original_model, family)
            if model:
                same_family.append((name, model))
        return same_family + [r for r in rungs if r not in same_family]

    def _rung_refusal(self, provider: str, client_name: str, model: str, excluded: Set[str],
                      original_model: Optional[str], money_run: bool) -> str:
        """Why this rung may not serve (``""`` = it may)."""
        from core.runtime_config import fallback_provider_allowlist
        if provider in excluded or client_name in excluded:
            return "excluded"
        allowed = fallback_provider_allowlist()
        if allowed is not None and provider not in allowed:
            return "not in LLM_FALLBACK_PROVIDERS"
        if self._credit_dead(provider):
            return "credit-dead"
        if money_run and original_model:
            refusal = _money_refusal(model, original_model)
            if refusal:
                return refusal
        return _price_refusal(provider, model, original_model)

    async def get_fallback_chat_model(
        self,
        exclude_providers: Optional[List[str]] = None,
        original_model: Optional[str] = None,
        temperature: float = 0.0,
        money_run: bool = False,
        **kwargs
    ) -> Optional[BaseChatModel]:
        """The first rung of the fallback ladder that may serve, on an isolated client.

        Args:
            exclude_providers: Providers to skip (the failed ones). Not changed.
            original_model: The failed model. Its family orders the ladder and its price
                guards each rung. None asks for any model, with no price rule.
            temperature: Temperature for generation
            money_run: The run is money-class, so it stays on a money-class model.
            **kwargs: Additional parameters to pass to the chat model

        Returns:
            A chat model the caller owns and closes, or None: the caller then waits and
            retries its own model.
        """
        from modules.llm.llm_factory import create_chat_model

        if not self._initialized:
            await self.initialize()

        excluded = set(exclude_providers or ())
        self.logger.info(
            f"Searching the fallback ladder (excluding: {sorted(excluded)}, "
            f"original: {original_model or 'unknown'})"
        )

        for client_name, fallback_model in self._fallback_ladder(original_model):
            provider = _provider_of(client_name)
            refusal = self._rung_refusal(
                provider, client_name, fallback_model, excluded, original_model, money_run)
            if refusal:
                self.logger.info(f"Fallback rung {provider}/{fallback_model} refused: {refusal}")
                continue

            client = self.clients.get(client_name) or await self._try_initialize_client(provider)
            if not client:
                self.logger.debug(f"Could not initialize {provider}, skipping")
                continue
            if not await self._test_client_health(client):
                self.logger.debug(f"Provider {provider} health check failed, skipping")
                continue

            iso_client = None
            try:
                # An ISOLATED client: adapters mutate model_type in place, and the shared
                # client above belongs to every session on this provider.
                iso_client = await self._create_isolated_client(provider, fallback_model)
                if iso_client is None:
                    self.logger.debug(f"Could not build isolated {provider} client for {fallback_model}, skipping")
                    continue
                chat_model = create_chat_model(provider=provider, model=fallback_model,
                                               temperature=temperature, llm_client=iso_client, **kwargs)
            except Exception as e:
                self.logger.warning(f"Failed to create chat model for {provider}: {e}")
                excluded.add(provider)
                close = getattr(iso_client, "cleanup", None)  # built for this rung only
                if callable(close):
                    await asyncio.gather(close(), return_exceptions=True)
                continue
            self.logger.info(f"Fallback rung: {provider}/{fallback_model} (original was: {original_model or 'unknown'})")
            return chat_model

        self.logger.warning(f"No fallback rung may serve. Excluded: {sorted(excluded)}")
        return None
