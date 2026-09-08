"""LLM response/provider helpers: provider detection, serving-provider resolution,
token-usage extraction.

Relocated from ``agents/task/utils.py`` to the LLM layer (S5, 2026-08-29) — they read
only ``modules.llm.model_registry`` and are needed by ``modules.llm.aux_metering``;
``agents.task.utils`` re-exports the three names for its callers.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("task.utils")


def detect_llm_provider(result: Any, model_name: Optional[str] = None) -> str:
    """Detect LLM provider from model name using the model_registry.

    Args:
        result: LLM response object (not used, kept for compatibility)
        model_name: Model name for detection

    Returns:
        Provider name ('openai', 'anthropic', 'gemini', 'deepseek', 'openrouter', 'llama', or 'generic')
    """
    if not model_name:
        return 'generic'
    
    try:
        # Use model_registry as the SINGLE SOURCE OF TRUTH (WS-2.3): the canonical
        # enum→string map lives there, so a new provider is added in one place and
        # GOOGLE→'gemini' can never drift back to 'google' here.
        from modules.llm.model_registry import get_model_config, canonical_provider_name

        model_config = get_model_config(model_name)
        if model_config and model_config.provider:
            return canonical_provider_name(model_config.provider, default='generic')

    except ImportError:
        logger.debug("model_registry not available for provider detection")

    return 'generic'


def resolve_serving_provider(llm: Any, model_name: Optional[str] = None) -> str:
    """Provider whose TOOL-SCHEMA shape a request must use.

    ``detect_llm_provider`` maps a model id to the vendor that ORIGINATED it,
    which is not necessarily the provider serving this session: the registry
    records ``glm-5`` under OpenRouter, but a ``zai-coding`` session serves it
    over the Anthropic-messages transport. Selecting schemas from the model id
    sent OpenAI-shaped tools to z.ai and earned a hard 422
    (``body.tools[0].name: Field required``), after which the agent degraded to
    emitting tool calls as prose.

    Schema shape follows the SERVING provider, so prefer the ProviderSpec the
    live client was built from — the same resolution
    ``modules.llm.adapters._client_provider_label`` uses for error framing.
    Built-in clients carry no ``_spec`` and fall back to model-name detection,
    so their behaviour is unchanged.
    """
    spec_name = getattr(
        getattr(getattr(llm, '_client', None), '_spec', None), 'name', None
    )
    # Only a genuine string names a ProviderSpec row — a Mock/stub llm in tests
    # auto-creates truthy attribute chains, and a non-str "name" leaking out of
    # here poisons every consumer (billing keys, schema lookup, telemetry).
    if isinstance(spec_name, str) and spec_name:
        return spec_name
    return detect_llm_provider(None, model_name)


def extract_token_usage(result: Any, provider: str) -> Dict[str, Optional[int]]:
    """Extract token usage from LLM response using provider-specific paths.

    Args:
        result: LLM response object (could be raw LLM response or structured output)
        provider: Provider name from detect_llm_provider

    Returns:
        Dictionary with token counts (total_tokens, prompt_tokens, completion_tokens, cached_tokens)
    """
    token_usage = {'total_tokens': None, 'prompt_tokens': None, 'completion_tokens': None, 'cached_tokens': None, 'cache_creation_tokens': None}

    try:
        # Handle structured output format: {'parsed': ..., 'raw': <llm_response>}
        actual_result = result
        if isinstance(result, dict) and 'raw' in result:
            # Structured output - extract the raw LLM response
            actual_result = result['raw']

        # Try AIMessage format first (usage_metadata attribute)
        if hasattr(actual_result, 'usage_metadata') and actual_result.usage_metadata:
            usage = actual_result.usage_metadata
            # Handle both dict and object formats (adapters may pass dict or object)
            if isinstance(usage, dict):
                # Dict format (from our adapters)
                token_usage['prompt_tokens'] = (
                    usage.get('input_tokens') or
                    usage.get('prompt_tokens')
                )
                token_usage['completion_tokens'] = (
                    usage.get('output_tokens') or
                    usage.get('completion_tokens')
                )
                token_usage['total_tokens'] = usage.get('total_tokens')
                token_usage['cached_tokens'] = usage.get('cache_read_input_tokens') or usage.get('cached_tokens')
                token_usage['cache_creation_tokens'] = usage.get('cache_creation_input_tokens')
            else:
                # Object format
                token_usage['prompt_tokens'] = (
                    getattr(usage, 'input_tokens', None) or
                    getattr(usage, 'prompt_tokens', None)
                )
                token_usage['completion_tokens'] = (
                    getattr(usage, 'output_tokens', None) or
                    getattr(usage, 'completion_tokens', None)
                )
                token_usage['cached_tokens'] = getattr(usage, 'cache_read_input_tokens', None)
            # Calculate total if not already set
            if token_usage['total_tokens'] is None and token_usage['prompt_tokens'] and token_usage['completion_tokens']:
                token_usage['total_tokens'] = token_usage['prompt_tokens'] + token_usage['completion_tokens']

        # Try response_metadata format
        elif hasattr(actual_result, 'response_metadata'):
            metadata = actual_result.response_metadata
            if 'token_usage' in metadata:
                usage = metadata['token_usage']
                token_usage['prompt_tokens'] = usage.get('prompt_tokens')
                token_usage['completion_tokens'] = usage.get('completion_tokens')
                token_usage['total_tokens'] = usage.get('total_tokens')
                token_usage['cached_tokens'] = usage.get('cached_tokens')

        # Direct attribute access based on known provider patterns
        elif provider == 'openai':
            # OpenAI format: result.usage.{total_tokens, prompt_tokens, completion_tokens}
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                token_usage['total_tokens'] = getattr(usage, 'total_tokens', None)
                token_usage['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cached_tokens', None)

        elif provider == 'anthropic':
            # Anthropic format: result.usage.{input_tokens, output_tokens}
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                token_usage['prompt_tokens'] = getattr(usage, 'input_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'output_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cache_read_input_tokens', None)
            # Sometimes Anthropic puts these directly on result
            elif hasattr(actual_result, 'input_tokens'):
                token_usage['prompt_tokens'] = getattr(actual_result, 'input_tokens', None)
                token_usage['completion_tokens'] = getattr(actual_result, 'output_tokens', None)

        else:
            # Generic fallback - try multiple common patterns
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                # Try OpenAI pattern first
                token_usage['total_tokens'] = getattr(usage, 'total_tokens', None)
                token_usage['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cached_tokens', None) or getattr(usage, 'cache_read_input_tokens', None)
                # Try Anthropic pattern if OpenAI didn't work
                if not token_usage['prompt_tokens']:
                    token_usage['prompt_tokens'] = getattr(usage, 'input_tokens', None)
                if not token_usage['completion_tokens']:
                    token_usage['completion_tokens'] = getattr(usage, 'output_tokens', None)

        # Calculate total if we have prompt + completion but no total
        if (token_usage['total_tokens'] is None and
            token_usage['prompt_tokens'] is not None and
            token_usage['completion_tokens'] is not None):
            token_usage['total_tokens'] = token_usage['prompt_tokens'] + token_usage['completion_tokens']

        # Ensure all values are proper integers or None
        for key in token_usage:
            if token_usage[key] is not None:
                try:
                    token_usage[key] = int(token_usage[key])
                except (ValueError, TypeError):
                    token_usage[key] = None

    except Exception as e:
        logging.getLogger('task.utils').debug(f"Error extracting token usage: {e}")

    return token_usage
