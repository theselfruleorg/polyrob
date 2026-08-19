"""Registry of LLM clients and available models.

DEPRECATED: Model lists now sourced from model_registry.py for single source of truth.
This file is kept for backward compatibility with create_llm_client() function.
"""

from typing import List
from modules.llm.model_registry import get_registry, ModelProvider

def _get_models_for_provider(provider: str) -> List[str]:
    """Get available models for a provider from model_registry.

    Args:
        provider: Provider name (openai, anthropic, google, deepseek, openrouter, custom)

    Returns:
        List of model names for the provider
    """
    try:
        # Map provider names to ModelProvider enum
        provider_map = {
            'openai': ModelProvider.OPENAI,
            'anthropic': ModelProvider.ANTHROPIC,
            'gemini': ModelProvider.GOOGLE,
            'google': ModelProvider.GOOGLE,
            'deepseek': ModelProvider.DEEPSEEK,
            'openrouter': ModelProvider.OPENROUTER,
            'nvidia': ModelProvider.NVIDIA,
        }

        provider_enum = provider_map.get(provider.lower())
        if not provider_enum:
            return []

        registry = get_registry()
        models = registry.list_models(provider=provider_enum, include_deprecated=False)
        return [m.name for m in models]
    except Exception:
        # Fallback to empty list if registry fails
        return []

class _LazyAvailableModels:
    """Dict-like provider→models view (values are real ``list``s).

    Proposal 024 (L0): unions the model_registry-backed lists with models
    DECLARED by ProviderSpec rows (``providers.yaml`` ``models:`` keys), so a
    user-declared provider's models are listable/ownable everywhere downstream
    (``polyrob model``, openai-compat ``_provider_owning``, config_store).
    Built lazily on first access and cached; ``reset()`` clears the snapshot
    (called by ``provider_spec.reset_provider_registry_cache``).
    """

    _LEGACY_PROVIDERS = ('anthropic', 'openai', 'deepseek', 'gemini', 'openrouter', 'nvidia')

    def __init__(self) -> None:
        self._snapshot = None

    def _ensure(self):
        if self._snapshot is None:
            snap = {}
            try:
                from modules.llm.provider_spec import get_specs, provider_registry_enabled
                registry_on = provider_registry_enabled()
            except Exception:
                registry_on = False
            if registry_on:
                for s in get_specs():
                    base = _get_models_for_provider(s.name)
                    declared = [m for m in s.models if m not in base]
                    snap[s.name] = base + declared
            else:
                for name in self._LEGACY_PROVIDERS:
                    snap[name] = _get_models_for_provider(name)
            self._snapshot = snap
        return self._snapshot

    def reset(self) -> None:
        self._snapshot = None

    # read-only mapping interface
    def __getitem__(self, key):
        return self._ensure()[key]

    def __contains__(self, key) -> bool:
        return key in self._ensure()

    def __iter__(self):
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __bool__(self) -> bool:
        return bool(self._ensure())

    def get(self, key, default=None):
        return self._ensure().get(key, default)

    def keys(self):
        return self._ensure().keys()

    def values(self):
        return self._ensure().values()

    def items(self):
        return self._ensure().items()


# Dict-like lazy view (was an eager dict; values are still plain lists).
AVAILABLE_MODELS = _LazyAvailableModels()

# Default models - hardcoded as policy decision (Nov 2025)
# NOTE: These are intentionally not derived from model_registry since defaults
# are a business/policy decision, not a capability question.
# Update manually when default model preferences change.
# NOTE: deepseek direct client DISABLED (Dec 2025) - use OpenRouter instead
DEFAULT_MODELS = {
    'anthropic': 'claude-sonnet-4-5',
    'openai': 'gpt-5',
    'gemini': 'gemini-2.5-flash',
    'deepseek': 'deepseek-chat',
    'openrouter': 'z-ai/glm-5.2',  # Z.AI GLM flagship (1M ctx); was x-ai/grok-4.3
    'nvidia': 'moonshotai/kimi-k2.6',  # free hosted NIM inference
}

def get_default_model(provider: str) -> str:
    """Get the default model for a provider.

    A per-provider env override ``POLYROB_<PROVIDER>_MODEL`` wins when set — so a
    deploy (esp. the headless ``polyrob telegram`` path, which otherwise has no
    model knob) can pin/swap the model with just an env change + restart, no code
    change or redeploy. Unset → the hardcoded ``DEFAULT_MODELS`` policy default
    (byte-identical). Example: ``POLYROB_OPENROUTER_MODEL=x-ai/grok-4.3``.

    Args:
        provider: Provider name

    Returns:
        Default model name for the provider
    """
    import os
    # Hyphenated provider names (zai-coding) map to underscores — a POSIX env
    # name can't contain '-' (UX assessment 2026-08-07, Q11).
    env_name = f"POLYROB_{provider.upper().replace('-', '_')}_MODEL"
    override = os.environ.get(env_name)
    if override and override.strip():
        return override.strip()
    # Proposal 024: a ProviderSpec may declare its own default (providers.yaml
    # ``default_model:`` — a new provider like ollama, or an explicit owner
    # override of a built-in). Built-in specs carry default_model=None, so with
    # no user file this is byte-identical to the DEFAULT_MODELS policy literal.
    try:
        from modules.llm.provider_spec import get_spec, provider_registry_enabled
        if provider_registry_enabled():
            spec = get_spec(provider)
            if spec is not None:
                if spec.default_model:
                    return spec.default_model
                # A declared-models row without default_model: its own first
                # model beats the openai literal below (requesting 'gpt-5' from
                # an Ollama endpoint is never right).
                if not spec.builtin and spec.models:
                    return spec.models[0]
    except Exception:
        pass
    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS['openai'])

# This function will be imported by llm_manager.py
def create_llm_client(name: str, config, container=None, model_type=None):
    """Create LLM client based on configuration.

    Args:
        name: Name of the client (anthropic, openai, llama, deepseek, gemini, openrouter)
        config: Bot configuration
        container: Optional dependency container
        model_type: Optional model type override

    Returns:
        Initialized LLM client instance
    """
    # Resolve the client class from PROVIDER_CONFIG (single source of truth).
    # Import client classes here to avoid circular imports at module level.
    from modules.llm.anthropic_client import AnthropicClient
    from modules.llm.openai_client import OpenAIClient
    from modules.llm.deepseek_client import DeepSeekClient
    from modules.llm.gemini_client import GeminiClient
    from modules.llm.openrouter_client import OpenRouterClient
    from modules.llm.nvidia_client import NvidiaClient
    from modules.llm.compat_clients import AnthropicCompatClient, OpenAICompatClient
    from modules.llm.responses_client import ResponsesCompatClient
    from modules.llm.model_registry import PROVIDER_CONFIG

    # Map client_class_name strings from PROVIDER_CONFIG to actual classes.
    # This indirection is required because PROVIDER_CONFIG stores names (not
    # class objects) to stay free of circular imports at definition time.
    _client_class_map = {
        'AnthropicClient': AnthropicClient,
        'OpenAIClient': OpenAIClient,
        'DeepSeekClient': DeepSeekClient,
        'GeminiClient': GeminiClient,
        'OpenRouterClient': OpenRouterClient,
        'NvidiaClient': NvidiaClient,
        # Proposal 024 generic clients — serve any ProviderSpec whose transport
        # is OpenAI- or Anthropic-compatible (user-declared providers.yaml rows).
        'OpenAICompatClient': OpenAICompatClient,
        'AnthropicCompatClient': AnthropicCompatClient,
        'ResponsesCompatClient': ResponsesCompatClient,
    }

    if name not in PROVIDER_CONFIG:
        # Distinguish "no such provider" from "this provider is declared but its
        # transport has no client yet". The second is a real, reachable state
        # (openai-codex / RESPONSES) and "Unknown LLM client type" sends the
        # user hunting for a typo that isn't there.
        try:
            from modules.llm.provider_spec import generic_client_class_name, get_spec
            spec = get_spec(name)
        except Exception:
            spec = None
        if spec is not None and spec.client_class_name is None \
                and generic_client_class_name(spec.transport) is None:
            raise ValueError(
                f"provider '{name}' speaks the '{spec.transport.value}' API, which "
                "POLYROB cannot serve yet — the connect flow and credential "
                "storage work, but there is no client for that wire format. "
                "Use a provider on chat_completions or anthropic_messages."
            )
        raise ValueError(f"Unknown LLM client type: {name}")

    entry = PROVIDER_CONFIG[name]
    client_class = _client_class_map[entry.client_class_name]
    
    # Get LLM config for this client
    llm_config = config.get_llm_config()
    client_config = llm_config.get(name, {})
    
    # Get model type from config or use provided override
    client_model = model_type or client_config.get('model') or get_default_model(name)
    
    # Create service name for this client
    service_name = f"{name}_client"
    
    # Create client instance
    client = client_class(config=config, name=service_name)
    
    # Set model type if provided or available
    if client_model:
        client.model_type = client_model
        
    # Register in container if available
    if container:
        # Check if service already exists
        if container.has_service(service_name):
            # Get existing client
            existing_client = container.get_service(service_name)
            
            # Only initialize if not already initialized
            if not getattr(existing_client, '_initialized', False):
                # Return existing client without re-registering
                return existing_client
        else:
            # Register new client
            container.register_service(service_name, client)

        # Register as generic services if they don't exist yet
        if not container.has_service('llm_client'):
            container.register_service('llm_client', client)
            
            # Also register as llm if not already registered
            if not container.has_service('llm'):
                container.register_service('llm', client)
        
        # Special case for Anthropic - prefer it if available
        elif name == 'anthropic' and container.has_service('llm_client'):
            current = container.get_service('llm_client')
            if current and current.__class__.__name__ != 'AnthropicClient':
                # Unregister existing services and register Anthropic
                container.unregister_service('llm_client')
                container.register_service('llm_client', client)
                
                if container.has_service('llm'):
                    container.unregister_service('llm')
                container.register_service('llm', client)
        
    return client 