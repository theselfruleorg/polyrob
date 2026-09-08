"""Read-only client/model inventory for :class:`~modules.llm.llm_manager.LLMManager`.

Extracted 2026-09-08 to bring `llm_manager.py` back under its shrink-only size
ratchet ceiling (the god-file decomposition rule in AGENTS.md: new behaviour gets
its own mixin rather than growing the manager). Pure introspection — every method
here reports what exists and mutates nothing, which is exactly why it separates
cleanly from the manager's lifecycle/fallback/selection half.

`LLMManager` composes this via MRO, so `self.clients`, `self.llm_config`,
`self.primary_client_name`, `self._initialized`, `self.initialize()` and
`self.logger` are the manager's own attributes; the mixin never owns state.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from modules.llm.llm_client_registry import AVAILABLE_MODELS, get_default_model
from modules.llm.model_registry import get_model_config

logger = logging.getLogger(__name__)

#: Used when a client reports no completion ceiling and the registry has no
#: opinion either. Conservative on purpose — the value it replaced (1000) silently
#: truncated long answers.
_FALLBACK_MAX_TOKENS = 8000


def _pricing_block(model_config: Any) -> Optional[Dict[str, Any]]:
    """The pricing sub-dict for one model, or None when the registry has no row."""
    if not model_config:
        return None
    return {
        "input_price": model_config.pricing.input_price,
        "output_price": model_config.pricing.output_price,
    }


class InventoryMixin:
    """`get_available_models` / `get_available_clients` — what the UI may offer."""

    async def get_available_models(
        self, provider: Optional[str] = None
    ) -> List[Tuple[str, str]]:
        """Flat list of ``(provider, model)`` for INITIALIZED providers only.

        Only initialized providers are reported, so the UI can never offer a
        provider whose client failed to come up.

        Args:
            provider: optional provider name to filter results.
        """
        if not self._initialized:
            await self.initialize()

        # Client names look like "openai_client" / "openai_fallback".
        initialized_providers = {
            name.replace("_client", "").replace("_fallback", "")
            for name in self.clients.keys()
        }

        if provider:
            if (provider not in initialized_providers
                    and f"{provider}_client" not in self.clients):
                self.logger.debug(
                    f"Provider '{provider}' requested but not initialized")
                return []
            wanted = {provider}
        else:
            wanted = initialized_providers

        # P0.6: the model list comes from the ONE catalog
        # (modules.llm.available_models), never from AVAILABLE_MODELS directly.
        # Same tuples for the same initialized providers — an initialized provider
        # always has a usable key, so `usable ∩ wanted == wanted`.
        from modules.llm.available_models import available_models as _build_models
        choices = _build_models(initialized_only=True, initialized_providers=wanted)
        return [(c.provider, c.model) for c in choices]

    async def get_available_clients(self) -> Dict[str, Dict[str, Any]]:
        """Metadata for every client: live ones first, then configured-but-unbuilt.

        Returns a dict keyed by client name, each carrying provider, model,
        initialization state, token limits, context window and pricing.
        """
        if not self._initialized:
            await self.initialize()

        result: Dict[str, Dict[str, Any]] = {}

        for name, client in self.clients.items():
            provider = name.replace("_client", "")
            model_config = None
            max_tokens = getattr(client, "max_tokens", None)
            if max_tokens is None:
                # Prefer the registry's ceiling for this model over a blind default.
                if getattr(client, "model_type", None):
                    model_config = get_model_config(client.model_type)
                    if model_config and model_config.max_completion_tokens:
                        max_tokens = model_config.max_completion_tokens
                if max_tokens is None:
                    max_tokens = _FALLBACK_MAX_TOKENS

            result[name] = {
                "name": name,
                "provider": provider,
                "model": client.model_type,
                "initialized": getattr(client, "_initialized", False),
                "is_primary": name == self.primary_client_name,
                "max_tokens": max_tokens,
                "temperature": getattr(client, "temperature", 0.7),
                "available_models": AVAILABLE_MODELS.get(provider, []),
                "context_window": model_config.context_window if model_config else None,
                "pricing": _pricing_block(model_config),
            }

        # Providers we hold config for but never built a client for.
        for provider in AVAILABLE_MODELS.keys():
            client_name = f"{provider}_client"
            if client_name in result:
                continue
            config_data = self.llm_config.get(provider, {})
            if not (config_data and "api_key" in config_data):
                continue
            model_name = config_data.get("model") or get_default_model(provider)
            model_config = get_model_config(model_name) if model_name else None
            result[client_name] = {
                "name": client_name,
                "provider": provider,
                "model": model_name,
                "initialized": False,
                "is_primary": False,
                "max_tokens": (model_config.max_completion_tokens
                               if model_config else _FALLBACK_MAX_TOKENS),
                "available_models": AVAILABLE_MODELS.get(provider, []),
                "context_window": model_config.context_window if model_config else None,
                "pricing": _pricing_block(model_config),
            }

        return result
