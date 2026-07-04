"""Live model hot-swap for a running Agent. Reprovisions the main chat model and
updates every model/provider SSOT the loop reads, so a swap is followed by billing,
native-tools reconciliation, token-counting, compaction and prompt-cache on the NEW
model. On a cross-provider switch the prompt cache resets (a model change always
breaks it).

SSOT updated on a successful swap (all AFTER the fallible LLM build, so a failed
build leaves the agent completely unchanged):
- ``self.llm``                        — the running chat model (billing/step loop)
- ``self.llm_provider``               — Agent-level provider label (billing reads it;
                                        see ``next_action_internal.py``)
- ``self.chat_model_library``         — adapter class name (native-tools detection)
- ``self.model_name`` / ``provider_name`` — Agent properties delegating to the
                                        MessageManager SSOT (the model_name setter
                                        re-detects the provider from the name; the
                                        provider_name setter then overrides it with
                                        the registry-canonical label)
- ``message_manager.model_name`` / ``provider_name`` — the SSOT the properties mirror
- ``message_manager.llm``             — the compaction/aux LLM (``compactor.py`` reads
                                        ``self.llm``) — otherwise post-swap compaction
                                        would still run/bill on the OLD model
- ``message_manager`` token budgets + ``compaction_manager`` — re-derived for the new
                                        context window via ``recalibrate_for_model``
- the pinned runtime-identity foundation line — refreshed so the agent reports the
                                        NEW model on the next turn
- native tools — reconciled for the new provider (best-effort, AFTER the SSOT block)

History-repair note: MessageManager's ``_validate_and_repair_tool_sequences`` (the
FiltersMixin method) is only ever called from ``get_messages_for_llm()`` on the
OUTBOUND path (see ``agents/task/agent/messages/retrieval.py``) — it repairs a fresh
copy per call and there is no code path that persists the repaired list back into
stored history. So a cross-provider swap needs no extra repair step here: the very
next ``get_messages_for_llm()`` call already re-repairs the stored history against
whatever tool-calling shape the new provider needs. Nothing to do but note it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ModelSwapMixin:
    """Adds ``swap_model`` — the shared hot-swap primitive Agent composes in."""

    def _detect_provider_for(self, model: str) -> Optional[str]:
        """Best-effort provider auto-detect for `model` via the model registry.

        Returns the canonical agent-facing provider string (e.g. "anthropic"), or
        None if the model isn't registered / detection fails for any reason. This
        mirrors the auto-detect branch in ``_create_llm_from_config_async`` so the
        SSOT provider label matches what that method will actually build.
        """
        try:
            from modules.llm.model_registry import get_registry, canonical_provider_name
            registry = get_registry()
            model_config = registry.get_model(model)
            if model_config:
                return canonical_provider_name(model_config.provider, default='openai')
        except Exception:
            pass
        return None

    async def swap_model(
        self,
        provider: Optional[str],
        model: str,
        *,
        preserve_history: bool = True,
    ) -> Dict[str, Any]:
        """Live-swap this agent's LLM to `provider`/`model`.

        `provider` may be falsy — in that case the provider is auto-detected from
        the model registry (needed by the OpenAI-compat per-request model path,
        which only gets a model string). On success every SSOT the running loop
        reads is updated in place (see the module docstring for the full list:
        self.llm/model_name/llm_provider/provider_name/chat_model_library,
        MessageManager.model_name/provider_name/llm + recalibrated token budgets +
        compaction manager) and native tools are reconciled for the new provider.
        On failure the agent is left completely unchanged and {"ok": False, ...} is
        returned.

        `preserve_history` is accepted for forward-compat with call sites that may
        want to reset conversation state on swap; today history is always kept
        (see the History-repair note in the module docstring) so the flag is
        currently a no-op.
        """
        prev = {
            "provider": getattr(self, "llm_provider", None) or getattr(self, "provider_name", None),
            "model": getattr(self, "model_name", None),
        }

        resolved_provider = provider or self._detect_provider_for(model)

        cfg: Dict[str, Any] = {"model": model}
        if provider:
            cfg["provider"] = provider

        try:
            new_llm = await self._create_llm_from_config_async(cfg)
        except Exception as e:
            self.logger.warning(f"model swap build failed for {resolved_provider or '?'}/{model}: {e}")
            new_llm = None

        if new_llm is None:
            return {
                "ok": False,
                "error": f"could not build {resolved_provider or '?'}/{model}",
                "previous": prev,
            }

        # Build succeeded but detection couldn't resolve a provider label up front
        # (e.g. falsy `provider` + an unregistered/aliased model that
        # _create_llm_from_config_async still managed to build via its own
        # fallback path). Don't leave None in a provider SSOT — label it
        # "unknown" rather than silently propagating None into billing/
        # native-tools reconciliation.
        if not resolved_provider:
            resolved_provider = "unknown"

        # Update every SSOT the running loop reads. All mutations happen ONLY
        # after the fallible build above, so a failed build leaves the agent
        # completely unchanged (see the early return).
        self.llm = new_llm
        self.llm_provider = resolved_provider
        self.chat_model_library = type(new_llm).__name__

        # model_name / provider_name are Agent properties delegating to the
        # MessageManager SSOT. Set model_name first (its setter re-detects the
        # provider FROM the name), then override provider_name with the
        # registry-canonical label. A single write updates both views (no
        # redundant second write straight to the manager).
        self.model_name = model
        self.provider_name = resolved_provider

        message_manager = getattr(self, "message_manager", None)
        if message_manager is not None:
            # Compaction/aux paths captured the ORIGINAL llm at init
            # (compactor.py reads self.llm) — repoint so post-swap compaction
            # runs/bills on the NEW model, not the pre-swap one.
            message_manager.llm = new_llm

            # Re-derive token budgets + the compaction manager for the new
            # model's context window (init-time values were sized for the OLD
            # model). Reuses the init formula; best-effort so a registry miss
            # never aborts the swap.
            _recal = getattr(message_manager, "recalibrate_for_model", None)
            if callable(_recal):
                try:
                    _recal(model)
                except Exception as e:
                    self.logger.debug(f"token/compaction recalibration after swap failed: {e}")

            # Refresh the pinned runtime-identity foundation line so the agent
            # reports the NEW model on the next turn (not the pre-swap one).
            _set_ident = getattr(message_manager, "set_runtime_identity", None)
            if callable(_set_ident):
                try:
                    _set_ident(model, resolved_provider)
                except Exception as e:
                    self.logger.debug(f"runtime-identity refresh after swap failed: {e}")

        try:
            self._reconcile_native_tools(resolved_provider)
        except Exception as e:
            self.logger.debug(f"native-tools reconciliation after model swap failed: {e}")

        # Cross-provider history repair: no-op by design — see module docstring.
        # The next get_messages_for_llm() call already repairs the outbound copy
        # of stored history against the new provider's tool-calling shape.

        self.logger.info(f"model swapped {prev['provider']}/{prev['model']} -> {resolved_provider}/{model}")

        return {
            "ok": True,
            "provider": resolved_provider,
            "model": model,
            "previous": prev,
        }
