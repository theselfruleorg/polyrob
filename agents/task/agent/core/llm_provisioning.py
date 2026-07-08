"""LLM provisioning / config mixin (roadmap P9 decomposition; code-motion from service.py).

LLM construction from a config dict (async core + the P4 sync bridge wrapper),
token-limit and tool-calling-method configuration, and native-tools reconciliation.
Moved verbatim off the ``Agent`` god-file; ``Agent`` composes
``LLMProvisioningMixin`` so the ``__init__`` profile/session-config paths and the
flow-efficiency call sites are unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class LLMProvisioningMixin:
    """LLM-from-config construction + token/tool-method config for Agent."""

    def _supports_streaming(self) -> bool:
        """Check if LLM provider supports streaming.

        WS-2.3: the whitelist is derived from the canonical provider set in
        model_registry (``STREAMING_PROVIDER_NAMES``) rather than a hand-rolled
        list, so it can never drift out of sync with ``detect_llm_provider`` again
        — the original Gemini-streaming bug was exactly that drift ('google' vs
        the canonical 'gemini'). The legacy 'google' alias is still accepted for
        defence, since ``provider_name`` is now guaranteed canonical anyway.
        """
        from modules.llm.model_registry import STREAMING_PROVIDER_NAMES
        name = self.provider_name.lower()
        return name in STREAMING_PROVIDER_NAMES or name == 'google'

    async def _create_llm_from_config_async(self, llm_config: Dict[str, Any], isolated: bool = False) -> Optional[Any]:
        """Create LLM using canonical LLMManager.get_chat_model() method.

        Uses the existing LLMManager utility which handles:
        - Client lookup and initialization
        - Automatic fallback to available providers
        - Token limits from model_registry
        - Native chat model creation

        Args:
            llm_config: LLM configuration dictionary with provider, model, etc.

        Returns:
            Chat model instance or None if creation fails
        """
        try:
            # Get LLM manager from container (single source of truth)
            llm_manager = self.container.get_service('llm')
            if not llm_manager:
                self.logger.error("LLMManager not available in container - cannot create LLM from config")
                return None

            model = llm_config.get('model', 'gpt-5')
            provider = llm_config.get('provider')

            # If no provider specified, detect from model name using model registry
            if not provider:
                from modules.llm.model_registry import get_registry, canonical_provider_name
                registry = get_registry()
                model_config = registry.get_model(model)
                if model_config:
                    # WS-2.3: canonical enum→string map is the single source of truth.
                    provider = canonical_provider_name(model_config.provider, default='openai')
                    self.logger.info(f"Auto-detected provider '{provider}' for model '{model}'")
                else:
                    provider = 'openai'  # Final fallback
                    self.logger.warning(f"Model '{model}' not found in registry, defaulting to provider 'openai'")

            temperature = llm_config.get('temperature', 0.0)

            # Use canonical method - handles all client management and wrapping.
            # isolated=True (compaction aux) builds a fresh, non-cached client so it
            # can't clobber the main agent's shared per-provider client.
            return await llm_manager.get_chat_model(
                provider=provider,
                model=model,
                temperature=temperature,
                isolated_client=isolated,
                request_timeout=60,
                max_retries=3,
                **llm_config.get('model_kwargs', {})
            )

        except Exception as e:
            self.logger.error(f"Error creating LLM from config: {e}", exc_info=True)
            return None

    def _create_llm_from_config(self, llm_config: Dict[str, Any], isolated: bool = False) -> Optional[Any]:
        """Sync wrapper for _create_llm_from_config_async (P4).

        Called from the synchronous Agent.__init__ profile/session-config paths,
        which run inside the async ``create_agent`` — so a loop is usually already
        running on this thread. Delegates to the centralized ``core.async_bridge``,
        which uses ONE persistent background loop instead of spawning a fresh loop +
        thread per call. That removes the per-creation thread churn and keeps async
        LLM clients bound to a live loop (no "Event loop is closed" on GC).

        NOTE: the ideal end state still removes this wrapper entirely by constructing
        the profile/session LLM in an async ``initialize()`` step rather than in
        ``__init__``; that constructor-lifecycle change is tracked separately (P4 tail).
        """
        from core.async_bridge import run_coroutine_sync
        return run_coroutine_sync(self._create_llm_from_config_async(llm_config, isolated=isolated), timeout=60)

    def _provision_aux_llm(self, task: str) -> Optional[Any]:
        """Build a cheap isolated aux model for `task`, or None (=> main model).

        Generalizes _provision_compaction_llm. ``task`` is one of
        compaction/judge/reflection (see agents/task/constants.py::resolve_aux_chain
        — the 3 real aux call sites; slot count is intentionally fixed). Walks the
        ordered candidate chain (primary model + per-task fallbacks, B5) and returns
        the first one that builds successfully. Isolated client so a same-provider
        aux can't clobber the main agent's shared client. Fail-open: None on error
        or if every candidate fails to build => caller uses the main model.
        """
        from agents.task.constants import resolve_aux_chain

        chain = resolve_aux_chain(task, getattr(self, "provider_name", None))
        if not chain:
            return None
        try:
            for idx, candidate in enumerate(chain):
                model = candidate.get("model")
                if not model:
                    continue
                config = {"model": model}
                provider = candidate.get("provider")
                if provider:
                    config["provider"] = provider
                # isolated=True: build the aux on a fresh, non-cached client so a
                # same-provider aux can't clobber the main agent's shared client
                # (model_type mutation).
                aux = self._create_llm_from_config(config, isolated=True)
                if aux:
                    self.logger.info(
                        f"Aux model provisioned for '{task}': {provider or 'auto'}/{model}"
                    )
                    return aux
                suffix = "; trying next candidate" if idx < len(chain) - 1 else ""
                self.logger.warning(
                    f"Aux candidate for '{task}'={provider or 'auto'}/{model} could not be built{suffix}"
                )
            self.logger.warning(
                f"All aux candidates for '{task}' failed; using the main model"
            )
            return None
        except Exception as e:
            self.logger.warning(f"Could not provision aux model for '{task}': {e}")
            return None

    async def _provision_aux_llm_async(self, task: str) -> Optional[Any]:
        """Async form of _provision_aux_llm (P2-9).

        Awaits the isolated-client build DIRECTLY instead of blocking the loop thread
        via run_coroutine_sync(timeout=60). Use from async call sites (output validation
        / background review / goal judge) so a slow client construction yields the loop
        instead of freezing every concurrent session for up to 60s. Same chain-walk +
        fail-open semantics as the sync form.
        """
        from agents.task.constants import resolve_aux_chain

        chain = resolve_aux_chain(task, getattr(self, "provider_name", None))
        if not chain:
            return None
        try:
            for idx, candidate in enumerate(chain):
                model = candidate.get("model")
                if not model:
                    continue
                config = {"model": model}
                provider = candidate.get("provider")
                if provider:
                    config["provider"] = provider
                aux = await self._create_llm_from_config_async(config, isolated=True)
                if aux:
                    self.logger.info(
                        f"Aux model provisioned (async) for '{task}': {provider or 'auto'}/{model}"
                    )
                    return aux
                suffix = "; trying next candidate" if idx < len(chain) - 1 else ""
                self.logger.warning(
                    f"Aux candidate for '{task}'={provider or 'auto'}/{model} could not be built{suffix}"
                )
            self.logger.warning(f"All aux candidates for '{task}' failed; using the main model")
            return None
        except Exception as e:
            self.logger.warning(f"Could not provision aux model (async) for '{task}': {e}")
            return None

    def _provision_compaction_llm(self) -> Optional[Any]:
        """A5/A1: cheap aux model used ONLY for `llm_compact_history`. Thin wrapper over
        _provision_aux_llm('compaction') — preserves the COMPACTION_MODEL / COMPACTION_AUTO_AUX
        knobs and all existing callers. Inert by default (None => main model)."""
        return self._provision_aux_llm("compaction")

    def set_token_limits(self, main_max_tokens: int = None, eval_max_tokens: int = None) -> None:
        """Set token limits for the main LLM.

        DEPRECATED: Token limits are now automatically configured during LLM creation
        via llm_factory which uses model_registry. This method is kept for
        backward compatibility but should not be needed.

        Args:
            main_max_tokens: Optional max tokens override
            eval_max_tokens: Deprecated (evaluation removed)
        """
        from modules.llm.model_registry import get_model_config

        # Get from model registry if not provided
        if main_max_tokens is None:
            config = get_model_config(self.model_name)
            main_max_tokens = config.max_completion_tokens if config else 16384

        # Only set if LLM doesn't already have it configured
        if hasattr(self.llm, 'max_tokens'):
            current_value = getattr(self.llm, 'max_tokens', None)
            if current_value is None or current_value == 0:
                self.llm.max_tokens = main_max_tokens
                self.logger.info(f"Set main LLM max_tokens to {main_max_tokens}")
            else:
                self.logger.debug(f"LLM already has max_tokens={current_value}, keeping existing value")

    def _get_model_max_completion_tokens(self, model_name: str) -> int:
        """Get max completion tokens from model registry.

        DEPRECATED: Use model_registry.get_model_config() directly instead.
        This method is kept for backward compatibility.
        """
        from modules.llm.model_registry import get_model_config
        config = get_model_config(model_name)
        completion_tokens = config.max_completion_tokens if config else 16384
        self.logger.debug(f"Using max_tokens={completion_tokens} for {model_name}")
        return completion_tokens

    def _reconcile_native_tools(self, provider: str) -> bool:
        """Intersect the user's native-tools preference with provider capability.

        Flow-efficiency D2-b: the effective value is what MessageManager is built
        with, so we ALSO re-assign self.use_native_tools here to keep the agent's own
        flag consistent. Otherwise _call_llm (reads message_manager.use_native_tools)
        and _add_tool_messages (reads self.use_native_tools) could take mismatched
        native vs synthetic paths and corrupt the AIMessage/ToolMessage pairing.

        Returns the effective (reconciled) value.
        """
        user_wants_native = getattr(self, 'use_native_tools', True)
        # Use Controller's high-level API instead of directly accessing registry
        provider_supports_native = self.controller.supports_native_tools(provider)
        effective = user_wants_native and provider_supports_native

        if user_wants_native and not provider_supports_native:
            self.logger.info(f"Provider {provider} does not support native tools, using JSON fallback")
        elif not user_wants_native:
            self.logger.info("Native tools disabled by user preference")

        # Keep the agent's own flag aligned with what MessageManager will use.
        self.use_native_tools = effective
        return effective

    def set_tool_calling_method(self, tool_calling_method: Optional[str]) -> Optional[str]:
        """Determine tool calling method based on LLM provider.

        Uses self.chat_model_library (adapter class name) to detect provider capabilities.
        All modern providers support function_calling (native tools).

        Args:
            tool_calling_method: Explicit method or 'auto' to auto-detect

        Returns:
            Tool calling method to use
        """
        if tool_calling_method == 'auto':
            # Enable function calling for all major providers
            if self.chat_model_library == 'ChatOpenAI':
                return 'function_calling'
            elif self.chat_model_library == 'ChatAnthropic':
                return 'function_calling'
            elif self.chat_model_library == 'ChatGoogleGenerativeAI':
                return 'function_calling'
            elif self.chat_model_library in ['ChatDeepSeek', 'DeepSeekChatAdapter']:
                return 'function_calling'
            else:
                # Default to function_calling for unknown providers
                self.logger.info(f"Unknown chat model library {self.chat_model_library}, defaulting to function_calling")
                return 'function_calling'
        else:
            return tool_calling_method
