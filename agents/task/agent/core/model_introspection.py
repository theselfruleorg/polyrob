"""Model-introspection mixin (roadmap P9 decomposition; code-motion from service.py).

Read-only helpers that ask "what can this model/LLM do?" — vision support,
provider detection, and model-name extraction. Moved verbatim off the ``Agent``
god-file; ``Agent`` composes ``ModelIntrospectionMixin`` (call sites in
llm_runner.py and __init__ unchanged via MRO).
"""
from __future__ import annotations

from typing import Any

from agents.task.utils import detect_llm_provider


class ModelIntrospectionMixin:
    """Vision/provider/model-name introspection for Agent."""

    def _current_llm_provider(self, model_name: str = "") -> str:
        """The SESSION's provider — authoritative from the live client, model
        inference only as fallback.

        Model-name inference misattributes a user-declared provider's model to
        a builtin (a `-p zai-coding` failure reported "provider openrouter
        failed" — UX assessment 2026-08-07, Q12). The live client knows who it
        is: a spec-backed generic client carries ``_spec.name``; builtin
        clients carry their service name. A candidate is only trusted when the
        registry knows it (so ``openai_fallback_client`` degrades to
        inference, not a phantom "openai_fallback" provider).
        """
        client = getattr(getattr(self, "llm", None), "_client", None)
        if client is not None:
            candidate = getattr(getattr(client, "_spec", None), "name", None)
            if not candidate:
                service = getattr(client, "name", "") or ""
                if isinstance(service, str) and service.endswith("_client"):
                    candidate = service[: -len("_client")]
            if candidate:
                try:
                    from modules.llm.profiles import get_profile
                    if get_profile(candidate) is not None:
                        return candidate
                except Exception:
                    pass
        return self._get_provider_from_model(model_name)

    def _get_provider_from_model(self, model_name: str) -> str:
        """Extract provider name from model name.

        Uses centralized detect_llm_provider from agents/task/utils.py.

        Args:
            model_name: Model name (e.g., 'gpt-5', 'claude-sonnet-4-5')

        Returns:
            Provider name (e.g., 'openai', 'anthropic', 'gemini', 'deepseek')
        """
        if not model_name:
            return 'unknown'

        # Use centralized utility - single source of truth
        provider = detect_llm_provider(None, model_name)
        return provider if provider != 'generic' else 'unknown'

    def _check_vision_support(self, model_name: str) -> bool:
        """Check if model supports vision using model_registry."""
        if not model_name:
            return False

        # Use model_registry as single source of truth
        from modules.llm.model_registry import get_model_config

        model_config = get_model_config(model_name)
        if model_config and hasattr(model_config, 'capabilities'):
            supports_vision = model_config.capabilities.supports_vision
            self.logger.debug(
                f"Model '{model_name}' vision support from registry: {supports_vision}"
            )
            return supports_vision

        # Fallback: If model not in registry, check client attribute
        if hasattr(self, 'llm') and hasattr(self.llm, '_client'):
            client = self.llm._client
            if hasattr(client, 'supports_vision'):
                supports_vision = client.supports_vision
                self.logger.debug(
                    f"Model '{model_name}' vision support from client: {supports_vision}"
                )
                return supports_vision

        # Final fallback: conservative default for unknown models
        # Better to fail safely than hallucinate on images
        self.logger.warning(
            f"Model '{model_name}' not in registry and no client info - assuming NO vision support"
        )
        return False

    def _extract_model_name(self, llm: Any) -> str:
        """Extract model name from LLM instance using centralized detection."""
        # Use provider detection to get model name
        provider = detect_llm_provider(llm)

        # Try to get specific model name
        for attr in ['model_name', 'model', '_model_name', 'deployment_name']:
            if hasattr(llm, attr):
                model = getattr(llm, attr, None)
                if model:
                    return model

        # Fallback to provider or class name
        return provider if provider != "unknown" else llm.__class__.__name__
