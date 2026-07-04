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

# Legacy constants - now dynamically sourced from model_registry
# FIXED (Nov 25, 2025): Changed from lambdas to direct function calls
# Previous lambdas caused AVAILABLE_MODELS['openai'] to return a function, not a list
def get_available_models_for_provider(provider: str) -> List[str]:
    """Get available models for a provider (callable version)."""
    return _get_models_for_provider(provider)

# FIXED: Eagerly evaluate model lists at import time for backward compatibility
# Code that accesses AVAILABLE_MODELS['openai'] expects a list, not a callable
AVAILABLE_MODELS = {
    'anthropic': _get_models_for_provider('anthropic'),
    'openai': _get_models_for_provider('openai'),
    'deepseek': _get_models_for_provider('deepseek'),
    'gemini': _get_models_for_provider('gemini'),
    'openrouter': _get_models_for_provider('openrouter'),
    'nvidia': _get_models_for_provider('nvidia'),
}

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
    override = os.environ.get(f"POLYROB_{provider.upper()}_MODEL")
    if override and override.strip():
        return override.strip()
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
    }

    if name not in PROVIDER_CONFIG:
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