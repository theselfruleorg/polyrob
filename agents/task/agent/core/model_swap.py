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
- ``message_manager`` token budgets — re-derived for the new context window via
                                        ``recalibrate_for_model``
- ``self.use_vision`` / ``message_manager._include_vision`` — re-resolved for the
                                        NEW model (095 W1a: an image turn's route note
                                        reads it)
- native tools — reconciled for the new provider (best-effort, AFTER the SSOT block)

What a model change rebuilds (identity line, system prompt, usage anchor) is
``_refresh_after_model_change(cause)`` — ONE helper for the owner's swap, the API
swap and every provider fallback, so a fallback refreshes the family note and the
JSON action list too, and only ``cause == "owner"`` renders "The owner switched you".

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


def _self_facts(agent, cause, previous_model):
    from agents.task.agent.core.state_delta import self_facts_text
    return self_facts_text(agent, cause, previous_model)


#: ``reasoning`` not given: keep the session's level.
_KEEP = object()


class ModelSwapMixin:
    """Adds ``swap_model`` — the shared hot-swap primitive Agent composes in."""

    def adopt_active_llm(self, new_llm: Any, model: str, provider: str) -> None:
        """Make an ALREADY-BUILT chat model the agent's active one, updating every
        model/provider SSOT the loop reads.

        Split out of ``swap_model`` so the two paths that change the running model
        share one implementation. The other caller is the automatic provider fallback
        in ``llm_runner.get_next_action``, which builds its own fallback client and
        used to set only ``self.llm``/``self.model_name`` — leaving ``self.llm_provider``
        (which the billing path reads, ``next_action_internal.py``) and
        ``message_manager.llm`` (which compaction reads) pointing at the ORIGINAL,
        just-failed provider. That mislabelled every post-fallback usage record and
        aimed compaction at the dead client.

        Callers must have completed any fallible build BEFORE calling this: every
        mutation here is unconditional.
        """
        self.llm = new_llm
        self.llm_provider = provider
        self.chat_model_library = type(new_llm).__name__

        # model_name / provider_name are Agent properties delegating to the
        # MessageManager SSOT. Set model_name first (its setter re-detects the
        # provider FROM the name), then override provider_name with the
        # registry-canonical label. A single write updates both views (no
        # redundant second write straight to the manager).
        self.model_name = model
        self.provider_name = provider

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

        # 095 W1a (SK-1): vision follows the NEW model. The configured
        # (pre-clamp) value is kept at construction; a blind fallback clears it,
        # a vision swap restores it. The identity line is NOT written here: the
        # caller's ``_refresh_after_model_change(cause)`` writes it ONCE.
        try:
            from agents.task.agent.core.construction import _resolve_session_vision
            configured = getattr(self, "_configured_use_vision",
                                 getattr(self, "use_vision", False))
            self.use_vision = _resolve_session_vision(self, bool(configured), new_llm)
            if message_manager is not None:
                message_manager._include_vision = bool(self.use_vision)
        except Exception as e:
            self.logger.debug(f"vision re-resolve after model change failed: {e}")

        try:
            self._reconcile_native_tools(provider)
        except Exception as e:
            self.logger.debug(f"native-tools reconciliation after model swap failed: {e}")

    def _refresh_after_model_change(self, cause: str, previous_model: Optional[str] = None,
                                    reason: Optional[str] = None) -> None:
        """095 W1a: what EVERY model change rebuilds — the swap (``owner``/``api``),
        a provider fallback (``fallback``, with the classified ``reason``) and the
        restore after a failed fallback (``restore``). Writes the identity line
        ONCE, rebuilds the system prompt (model-family note, JSON action list) and
        drops the provider usage anchor. Each step is best-effort."""
        manager = getattr(self, "message_manager", None)
        if manager is None:
            return
        model = getattr(self, "model_name", None)
        provider = getattr(self, "llm_provider", None) or getattr(self, "provider_name", None)
        switched_from = previous_model if cause in ("owner", "api", "fallback") else None
        if cause == "owner" and switched_from:
            from agents.task.model_pin_state import note_switch
            note_switch(getattr(self, "session_id", None), model, switched_from)
        for name, call in (
            ("identity", lambda: manager.set_runtime_identity(
                model, provider, switched_from=switched_from, cause=cause, reason=reason)),
            ("system prompt", lambda: manager.rebuild_system_prompt(
                getattr(self, "use_native_tools", None))),
            ("usage anchor", lambda: manager.reset_usage_anchor(f"model change ({cause})")),
            # 095 W4: the change also rides the <state-delta> tail (latest turn).
            ("state delta", lambda: manager.update_state_delta({"self_facts": _self_facts(
                self, cause, previous_model)})),
        ):
            try:
                call()
            except Exception as e:
                self.logger.debug(f"{name} refresh after model change failed: {e}")

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
        reasoning: Any = _KEEP,
        cause: str = "owner",
    ) -> Dict[str, Any]:
        """Live-swap this agent's LLM to `provider`/`model`.

        `provider` may be falsy — in that case the provider is auto-detected from
        the model registry (needed by the OpenAI-compat per-request model path,
        which only gets a model string). On success every SSOT the running loop
        reads is updated in place (see the module docstring), plus (094):

        * the new client is ISOLATED and keeps the old one's temperature and its
          per-session OpenRouter sort override;
        * the system prompt is rebuilt for the new model (family note, JSON action
          list when native tools are off) unless a profile supplied it;
        * the runtime-identity line names the model it was switched from and WHO
          switched it (``cause``: ``owner`` — REPL ``/model``, the 094 pin — or
          ``api`` — an OpenAI-compat request model);
        * the aux models (compaction, reflection, the lazy judge) are provisioned
          again, so a slot that follows the main model follows the swap;
        * the provider's usage anchor (the F6 gauge) is dropped;
        * ``reasoning`` (when given) becomes the session's reasoning level
          (``agents/task/model_pin_state.py``, read on each main call).

        On failure the agent is left completely unchanged and {"ok": False, ...} is
        returned. `preserve_history` is accepted for forward-compat; history is
        always kept (see the History-repair note in the module docstring).
        """
        prev = {
            "provider": getattr(self, "llm_provider", None) or getattr(self, "provider_name", None),
            "model": getattr(self, "model_name", None),
        }

        resolved_provider = provider or self._detect_provider_for(model)
        old_llm = getattr(self, "llm", None)

        cfg: Dict[str, Any] = {"model": model}
        if provider:
            cfg["provider"] = provider
        temperature = getattr(old_llm, "_default_temperature", None)
        if isinstance(temperature, (int, float)):
            cfg["temperature"] = float(temperature)

        try:
            new_llm = await self._create_llm_from_config_async(cfg, isolated=True)
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

        _carry_provider_sort(old_llm, new_llm)
        # 094: a client an EARLIER swap built is this session's own — close it once
        # it is replaced (the shared per-provider client is never closed here).
        _own_old = old_llm is not None and old_llm is getattr(self, "_swap_built_llm", None)

        # Update every SSOT the running loop reads. All mutations happen ONLY
        # after the fallible build above, so a failed build leaves the agent
        # completely unchanged (see the early return).
        self.adopt_active_llm(new_llm, model, resolved_provider)
        self._swap_built_llm = new_llm
        if _own_old:
            await _close_llm(old_llm)
        if reasoning is not _KEEP:
            _set_level(self, reasoning)
        self._after_swap(model, resolved_provider, prev.get("model"), cause=cause)

        self.logger.info(f"model swapped {prev['provider']}/{prev['model']} -> {resolved_provider}/{model}")

        return {
            "ok": True,
            "provider": resolved_provider,
            "model": model,
            "previous": prev,
        }

    def _after_swap(self, model: str, provider: str, previous_model: Optional[str],
                    cause: str = "owner") -> None:
        """094: a deliberate swap = the shared model-change refresh + the aux
        models (compaction, reflection, the lazy judge) on the new main chain."""
        self._refresh_after_model_change(cause, previous_model)
        try:
            self._reprovision_aux_models()
        except Exception as e:
            self.logger.debug(f"aux re-provision after swap failed: {e}")

    def _reprovision_aux_models(self) -> None:
        """Compaction + reflection on the NEW main model's chain; the judge lazily."""
        manager = getattr(self, "message_manager", None)
        if manager is not None:
            try:
                manager.aux_llm = self._provision_compaction_llm()
            except Exception:
                manager.aux_llm = None
        self._judge_llm = None
        from core.config_policy import reflection_llm_enabled_default
        tcm = getattr(self, "task_context_manager", None)
        session_id = getattr(self, "session_id", "") or ""
        if not (tcm is not None and session_id and reflection_llm_enabled_default()
                and hasattr(tcm, "set_reflection_context")):
            return
        current = (getattr(tcm, "_reflection_by_session", {}) or {}).get(session_id) or {}
        tcm.set_reflection_context(session_id, self._provision_aux_llm("reflection"),
                                   current.get("meter_ctx"))

    # --- 094: the owner's switch, applied at a step boundary -----------------

    def request_model_pin(self, pin: Dict[str, Any]) -> None:
        """Queue a switch: ``{"provider", "model", "reasoning"?, "on_applied"?}``.
        It is applied by :meth:`apply_pending_model_pin` at the next step boundary
        (the user-message drain), never during an LLM call. A newer request
        replaces an unapplied one."""
        self._pending_model_pin = dict(pin)

    def cancel_model_pin(self) -> None:
        """Drop a queued, unapplied switch (``/model reset`` back onto the live model)."""
        self._pending_model_pin = None

    def pending_model_pin(self) -> Optional[Dict[str, Any]]:
        return getattr(self, "_pending_model_pin", None)

    async def apply_pending_model_pin(self) -> Optional[Dict[str, Any]]:
        """Apply a queued switch (the swap result) or None when nothing is queued.
        ``on_applied(result)`` runs after it (persist + record + tell the owner)."""
        pin = getattr(self, "_pending_model_pin", None)
        if not pin:
            return None
        self._pending_model_pin = None
        reasoning = pin.get("reasoning", _KEEP)
        if pin.get("model") in (None, "", getattr(self, "model_name", None)) and (
                not pin.get("provider") or pin.get("provider") == getattr(self, "llm_provider", None)):
            # Same model: only the reasoning level changes — no rebuild, no cache loss.
            if reasoning is not _KEEP:
                _set_level(self, reasoning)
            result = {"ok": True, "provider": getattr(self, "llm_provider", None),
                      "model": getattr(self, "model_name", None), "unchanged": True,
                      "previous": {"provider": getattr(self, "llm_provider", None),
                                   "model": getattr(self, "model_name", None)}}
        else:
            try:
                result = await self.swap_model(pin.get("provider"), pin["model"],
                                               reasoning=reasoning, cause="owner")
            except Exception as e:  # noqa: BLE001 — a failed switch keeps the old model
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        callback = pin.get("on_applied")
        if callable(callback):
            try:
                await callback(result)
            except Exception as e:
                self.logger.debug(f"model pin on_applied failed: {e}")
        return result


async def _close_llm(llm: Any) -> None:
    client = getattr(llm, "_client", None)
    cleanup = getattr(client, "_cleanup", None)
    if callable(cleanup):
        try:
            await cleanup()
        except Exception:
            pass


def _set_level(agent: Any, level: Optional[str]) -> None:
    from agents.task.model_pin_state import set_session_reasoning
    set_session_reasoning(getattr(agent, "session_id", None), level)


def _carry_provider_sort(old_llm: Any, new_llm: Any) -> None:
    """Keep a per-session OpenRouter sort override (057 WS-B) across the swap."""
    try:
        from modules.llm.openrouter_routing import _SORT_ATTR, set_provider_sort
        old = getattr(old_llm, "_client", None) or old_llm
        sort = getattr(old, _SORT_ATTR, None)
        if sort:
            set_provider_sort(getattr(new_llm, "_client", None) or new_llm, sort)
    except Exception:
        pass
