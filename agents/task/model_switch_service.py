"""094: the ONE ``/model`` pipeline every owner seat calls — parse, resolve,
validate, guard, then pin and swap at the next step boundary.

* parse + aliases + guard rule: ``core/model_switch.py`` (pure);
* the owner's default + aliases: ``core/model_pins.py``;
* the live OpenRouter list (exists? tools? price? window?): ``modules/llm/openrouter_catalog.py``;
* the swap: ``Agent.request_model_pin`` → applied in the user-message drain
  (``agents/task/agent/core/user_ingress.py``) by ``swap_model``.

The model is ONE owner setting: ``/model <name>`` (a picker tap too) pins THIS chat's
session (its record's ``request.model/provider`` + ``model_pin``; survives a restart) AND
saves the owner's default, so ``/new`` and every new owner chat start on it; ``default`` is
a no-op word. ``/model reset`` clears both. The operator env (``DEFAULT_MODEL``) stays the floor and is never written here,
so the systemd pin can never refuse a chat switch.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from core import model_switch as ms

logger = logging.getLogger(__name__)


@dataclass
class Plan:
    provider: str
    model: str
    via: str
    reasoning: Optional[str]
    default: bool
    price: Optional[Tuple[float, float]] = None
    cached: Optional[float] = None
    context: int = 0
    verified: str = ""      # "" = verified; else why it was not
    reasoning_ok: bool = True   # the model takes a reasoning setting (OpenRouter list)


# --- reads ------------------------------------------------------------------

def _known_providers():
    from modules.llm.profiles import PROFILES
    return list(PROFILES)


def _keyed_providers():
    try:
        from modules.llm.profiles import providers_with_keys
        return providers_with_keys()
    except Exception:
        return []


def _operator_provider() -> str:
    from core.runtime_config import canonicalize_provider, operator_provider_pin
    pin = operator_provider_pin()
    if pin:
        return canonicalize_provider(pin) or pin
    keyed = _keyed_providers()
    return keyed[0] if keyed else ""


async def _model_facts(provider: str, model: str):
    """``(price, cached, context, tools, verified_note, found)`` for one pair."""
    if (provider or "").lower() == "openrouter":
        from modules.llm import openrouter_catalog as oc
        entry, read = await asyncio.to_thread(oc.lookup, model)
        if entry is not None:
            oc.register_live_model(entry)
            return entry.price, entry.cached_price, entry.context, entry.tools, "", True
        if read:
            return None, None, 0, False, "", False
        return None, None, 0, True, "the OpenRouter model list could not be read", True
    try:
        from modules.llm.model_registry import get_model_config_exact
        cfg = get_model_config_exact(model)
    except Exception:
        cfg = None
    if cfg is None:
        return None, None, 0, True, f"'{model}' is not in my model list for {provider}", True
    pr = cfg.pricing
    return ((pr.input_price, pr.output_price), pr.cached_input_price, cfg.context_window,
            cfg.capabilities.supports_function_calling, "", True)


#: The providers whose client maps a session reasoning level to the wire.
REASONING_PROVIDERS = ("openrouter", "deepseek")


def _token_level_refused(provider: Optional[str], level: Optional[str]) -> Optional[str]:
    """DeepSeek has no token budget, so a count would show a setting that nothing sends."""
    if (provider or "").lower() == "deepseek" and str(level or "").isdigit():
        return "❌ DeepSeek takes reasoning=off|on|low|medium|high, not a token count. Nothing changed."
    return None


async def _reasoning_ok(provider: Optional[str], model: Optional[str]) -> bool:
    """False only when the OpenRouter list says the model has no reasoning setting
    (a session level is then never sent to it); unknown = True."""
    if (provider or "").lower() != "openrouter" or not model:
        return True
    from modules.llm import openrouter_catalog as oc
    entry, _read = await asyncio.to_thread(oc.lookup, model, refresh_on_miss=False)
    return entry.reasoning if entry is not None else True


# --- the plan ------------------------------------------------------------------

async def plan_switch(cmd: ms.ModelCommand, *, user_id: str,
                      current: Tuple[Optional[str], Optional[str]] = (None, None),
                      ) -> Tuple[Optional[Plan], str]:
    """Resolve + validate + guard a ``set`` command. ``(plan, "")`` or ``(None, why)``."""
    from core import model_pins
    from modules.llm.profiles import get_profile
    res = ms.resolve_name(cmd.name, explicit_provider=cmd.provider,
                          operator_provider=_operator_provider(),
                          keyed_providers=_keyed_providers(),
                          known_providers=_known_providers(),
                          owner_aliases=model_pins.aliases(user_id))
    if res.error:
        return None, f"❌ {res.error} Nothing changed."
    if get_profile(res.provider) is None:
        return None, f"❌ Unknown provider '{res.provider}'. Nothing changed."
    keyed = {p.lower() for p in _keyed_providers()}
    if keyed and res.provider.lower() not in keyed:
        return None, f"❌ No API key for {res.provider} on this box. Nothing changed."
    price, cached, context, tools, note, found = await _model_facts(res.provider, res.model)
    if not found:
        from modules.llm import openrouter_catalog as oc
        near = oc.nearest(res.model)
        hint = f" Nearest: {', '.join(near)}." if near else ""
        return None, f"❌ '{res.model}' is not on {res.provider}.{hint} Nothing changed."
    if not tools:
        return None, (f"❌ {res.model} cannot call tools, and every turn of mine uses tools. "
                      "Nothing changed.")
    plan = Plan(res.provider, res.model, res.via, cmd.reasoning, cmd.default,
                price=price, cached=cached, context=context, verified=note,
                reasoning_ok=await _reasoning_ok(res.provider, res.model))
    if cmd.reasoning and not plan.reasoning_ok:
        return None, (f"❌ {res.model} has no reasoning setting; switch without reasoning=. "
                      "Nothing changed.")
    refused = _token_level_refused(res.provider, cmd.reasoning)
    if refused:
        return None, refused
    return plan, ""


def describe(plan: Plan) -> str:
    bits = [f"{plan.model} ({plan.provider})"]
    if plan.via and plan.via != "id":
        bits.append(f"— {plan.via}")
    lines = [" ".join(bits),
             f"Price: {ms.fmt_price(plan.price, plan.cached)}"
             + (f" · context {plan.context:,}" if plan.context else "")]
    if plan.reasoning:
        lines.append(f"Reasoning: {plan.reasoning}"
                     + ("" if plan.provider in REASONING_PROVIDERS else " (sent on OpenRouter and DeepSeek only)"))
    if plan.verified:
        lines.append(f"⚠️ Not verified: {plan.verified}. The first reply shows if it works.")
    return "\n".join(lines)


# --- the live session -------------------------------------------------------------

def _bound_session_id(task_agent: Any, chat_key: Optional[str], hint: Optional[str]) -> Optional[str]:
    """The session this chat's NEXT message reaches — or None when the binding has
    crossed the idle/daily boundary (the next message starts a fresh session, so a
    pin on the old one would be lost; it is queued for the chat instead)."""
    try:
        container = getattr(task_agent, "container", None)
        registry = container.get_service("session_chat_registry") if container else None
        row = registry.resolve(chat_key) if (registry is not None and chat_key) else None
    except Exception:
        row = None
    if row is None:
        return hint
    try:
        import time
        from core.surfaces.config import SurfaceConfig
        from core.surfaces.session_policy import should_start_fresh, with_agent_activity
        fresh, _why = should_start_fresh(
            with_agent_activity(row, container), now=time.time(),
            idle_minutes=SurfaceConfig.session_idle_minutes(),
            daily_hour=SurfaceConfig.session_reset_hour(),
            mode=SurfaceConfig.session_reset_mode())
        if fresh:
            return None
    except Exception:
        logger.debug("boundary check skipped for %s", chat_key, exc_info=True)
    return row.get("session_id") or hint


_RECORD_KEYS = ("request", "config", "model", "operator_pin", "model_pin")


def _snapshot(task_agent: Any, session_id: Optional[str]) -> dict:
    """The record keys a switch writes, as they are now. A key the record does not
    hold yet is kept as its empty value, so a failed switch never leaves its own pin
    (or its reasoning level) behind on a session that was never pinned."""
    if not session_id:
        return {}
    info = _record(task_agent, session_id)
    return {k: (info[k] if k in info else ({} if k in ("request", "config") else None))
            for k in _RECORD_KEYS}


def _switch_snapshot(task_agent: Any, session_id: Optional[str], agent: Any) -> dict:
    """The record to restore when a queued switch fails. A switch that replaces a
    queued, unapplied one restores the record from BEFORE the first one — the first
    pin never ran, so it must never be what a failure puts back."""
    pending = agent.pending_model_pin() if agent is not None else None
    if pending and isinstance(pending.get("snapshot"), dict):
        return pending["snapshot"]
    return _snapshot(task_agent, session_id)


def _restore_default(user_id, failed: dict, prev: Optional[dict]) -> None:
    """A failed swap must not stay the model of every new chat: put back the default the
    switch replaced — only while the store still holds the failed model (a later switch
    from another seat wins)."""
    from core import model_pins
    now = model_pins.owner_default(user_id) or {}
    if (now.get("provider"), now.get("model")) != (failed.get("provider"), failed.get("model")):
        return
    ok, err = (model_pins.set_default(user_id, prev["provider"], prev["model"],
                                      prev.get("reasoning")) if prev
               else model_pins.clear_default(user_id))
    if not ok:
        logger.warning("default not restored after a failed switch: %s", err)


def _failure_callback(task_agent, user_id, sid, snapshot, attrs, target, current_model,
                      default_undo: Optional[Tuple[dict, Optional[dict]]] = None):
    """``on_applied`` for a queued swap: success records it; a failed build puts the
    session record back EXACTLY as it was, puts back the saved default the switch
    replaced (``default_undo`` = ``(the failed pair, the previous default)``) and tells
    the owner."""
    async def _on_applied(result: dict) -> None:
        if result.get("ok"):
            _emit("model_switched", user_id, sid, attrs)
            return
        if default_undo is not None:
            try:
                _restore_default(user_id, *default_undo)
            except Exception:
                logger.warning("default not restored after a failed switch", exc_info=True)
        if sid and snapshot:
            try:
                task_agent.session_manager.update_session_metadata(sid, dict(snapshot))
            except Exception:
                logger.warning("model pin record not restored for %s", sid, exc_info=True)
        _emit("model_switch_failed", user_id, sid, {**attrs, "error": str(result.get("error"))[:200]})
        _notify_owner(task_agent, user_id, f"⚠️ The switch to {target} failed "
                      f"({result.get('error')}); I stay on {current_model}.")
    return _on_applied


def _live_agent(task_agent: Any, session_id: Optional[str]):
    orch = task_agent.get_orchestrator(session_id) if (session_id and hasattr(
        task_agent, "get_orchestrator")) else None
    agents = getattr(orch, "agents", None) or {}
    agent = next(iter(agents.values()), None) if agents else None
    return agent if agent is not None and hasattr(agent, "request_model_pin") else None


def _record(task_agent: Any, session_id: str) -> dict:
    try:
        return task_agent.session_manager.get_session_info(session_id) or {}
    except Exception:
        return {}


def _persist_session_pin(task_agent: Any, session_id: str, provider: str, model: str,
                         reasoning: Optional[str], seat: str, *, follows_operator: bool = False,
                         switched_from: Optional[str] = None, chat_pin: bool = True) -> None:
    """The session record carries the pin: a recreate builds on it, B-4 keeps it.
    ``follows_operator`` (a reset onto ``DEFAULT_MODEL``) stamps ``operator_pin`` so the
    session follows the next operator change again (B-4). ``switched_from`` + its
    time ride the pin (095 W1a) so a reload still says who switched the model.
    ``chat_pin=False`` (a reset) records the model but no chat pin: a reset is not
    "this chat until /new", so the rollover does not carry the old default on."""
    import time
    info = _record(task_agent, session_id)
    pair = {"provider": provider, "model": model}
    pin = {**pair, "set_at": int(time.time()), "seat": seat}
    if reasoning:
        pin["reasoning"] = reasoning
    old = info.get("model_pin") or {}
    if switched_from and switched_from != model:
        pin["switched_from"], pin["switched_at"] = switched_from, pin["set_at"]
    elif old.get("model") == model and old.get("switched_from"):
        pin["switched_from"] = old["switched_from"]
        pin["switched_at"] = old.get("switched_at") or old.get("set_at")
    try:
        task_agent.session_manager.update_session_metadata(session_id, {
            "request": {**(info.get("request") or {}), **pair},
            "config": {**(info.get("config") or {}), **pair},
            "model": model, "operator_pin": dict(pair) if follows_operator else None,
            "model_pin": pin if (chat_pin and not follows_operator) else None})
    except Exception:
        logger.warning("model pin not persisted for %s", session_id, exc_info=True)


def _emit(kind: str, user_id: str, session_id: str, attrs: dict) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="model_switch", user_id=user_id, session_id=session_id or "",
             attrs=attrs)
    except Exception:
        pass


def owner_chat_pin(chat_key: Optional[str], user_id: str,
                   task_agent: Any = None) -> Optional[dict]:
    """094: the pin a NEW owner chat starts on — a switch queued for this chat whose
    default could not be saved, else the owner's saved model (every switch saves it, so
    a rollover, ``/new`` and another seat all read the newest choice) — or None (the
    operator pin)."""
    from agents.task.model_pin_state import take_chat_pin
    from core.model_pins import chat_pin_for
    return take_chat_pin(chat_key) or chat_pin_for(user_id)


def _raw_bound_sid(task_agent: Any, chat_key: Optional[str]) -> Optional[str]:
    """The session this chat is bound to, past the idle/daily boundary too."""
    if task_agent is None or not chat_key:
        return None
    try:
        container = getattr(task_agent, "container", None)
        registry = container.get_service("session_chat_registry") if container else None
        row = registry.resolve(chat_key) if registry is not None else None
        return (row or {}).get("session_id") or None
    except Exception:
        logger.debug("binding not read for %s", chat_key, exc_info=True)
        return None


def start_with_pin(task_agent: Any, session_id: Optional[str], pin: Optional[dict]) -> None:
    """After a pinned owner chat is created: its reasoning level + its record."""
    if not (session_id and pin):
        return
    from agents.task.model_pin_state import set_session_reasoning
    set_session_reasoning(session_id, pin.get("reasoning"))
    _persist_session_pin(task_agent, session_id, pin["provider"], pin["model"],
                         pin.get("reasoning"), "start")


# --- the verb ------------------------------------------------------------------------

async def model_command(task_agent: Any, *, user_id: str, words, chat_key: Optional[str] = None,
                        session_id: Optional[str] = None, seat: str = "telegram",
                        agent: Any = None) -> str:
    """``/model …`` on every owner seat (Telegram, console, the REPL). Returns the reply
    text. ``agent`` names the live agent when the seat holds it (the REPL); a switch it
    queues is applied by the seat at once (``agent.apply_pending_model_pin``)."""
    from core import model_pins
    cmd = ms.parse_model_args(words)
    if cmd.action == "error":
        return cmd.error
    if cmd.action == "aliases":
        table, err = model_pins.aliases_read(user_id)
        return "\n".join(ms.alias_lines(table, read_error=err))
    if cmd.action == "alias":
        target = None if cmd.alias_target == "-" else cmd.alias_target
        ok, err = model_pins.set_alias(user_id, cmd.name, target)
        if not ok:
            return f"❌ Alias not saved: {err}"
        return (f"✅ Alias {cmd.name} → {target}." if target else f"✅ Alias {cmd.name} removed.")
    if cmd.action == "choice" or cmd.tier:   # 121: an owner setting, not this chat
        from agents.task.model_settings import settings_command
        return await settings_command(cmd, user_id, seat)
    # The model is the owner's setting: a switch moves the chat in use AND is saved as the
    # default, so /new and every new chat keep it (`default` is still accepted, a no-op).
    cmd.default = True
    sid = _bound_session_id(task_agent, chat_key, session_id)
    if agent is None or not hasattr(agent, "request_model_pin"):
        agent = _live_agent(task_agent, sid)
    if cmd.action == "show":
        return await _show(task_agent, user_id, sid, agent, chat_key)
    if cmd.action == "reset":
        return await _reset(task_agent, user_id, sid, agent, cmd, seat, chat_key)
    current = _current(task_agent, sid, agent)
    if cmd.action == "reasoning":
        return await _set_reasoning(task_agent, user_id, sid, agent, cmd, current, seat)
    plan, err = await plan_switch(cmd, user_id=user_id, current=current)
    if plan is None:
        return err
    return _apply(task_agent, user_id, sid, agent, chat_key, plan, current, seat)


def _current(task_agent, sid, agent) -> Tuple[Optional[str], Optional[str]]:
    if agent is not None:
        return (getattr(agent, "llm_provider", None) or getattr(agent, "provider_name", None),
                getattr(agent, "model_name", None))
    if sid:
        req = _record(task_agent, sid).get("request") or {}
        return req.get("provider"), req.get("model")
    return None, None


def _apply(task_agent, user_id, sid, agent, chat_key, plan: Plan, current, seat) -> str:
    from core import model_pins
    from agents.task.model_pin_state import queue_chat_pin
    prev = model_pins.owner_default(user_id)
    # A level the owner set (this chat's, else the saved one) rides a switch onto a model
    # that takes one; a switch never drops it silently.
    reasoning = plan.reasoning
    if reasoning is None and plan.reasoning_ok:
        reasoning = (_keep_level(sid) if sid else None) or (prev or {}).get("reasoning")
    ok, err = model_pins.set_default(user_id, plan.provider, plan.model, reasoning)
    if not ok and agent is None and not sid and not chat_key:
        return f"❌ Default not saved ({err}). Nothing changed."
    # An unwritable store never blocks the switch of the chat in use: it is named.
    scope = ("this chat and every new chat" if ok
             else f"this chat only — ⚠️ NOT saved for new chats ({err})")
    attrs = {"from": f"{current[0] or '?'}/{current[1] or '?'}",
             "to": f"{plan.provider}/{plan.model}", "via": plan.via, "seat": seat,
             "default": ok, "reasoning": reasoning or ""}
    if agent is not None:
        pending = agent.pending_model_pin()
        # A switch that replaces a queued, unapplied one undoes to the default from
        # BEFORE the first (that one never ran), as the record snapshot does.
        before = pending["prev_default"] if pending and "prev_default" in pending else prev
        snapshot = _switch_snapshot(task_agent, sid, agent)
        undo = ({"provider": plan.provider, "model": plan.model}, before) if ok else None
        _on_applied = _failure_callback(task_agent, user_id, sid, snapshot,
                                        attrs, plan.model, current[1], undo)
        agent.request_model_pin({"provider": plan.provider, "model": plan.model,
                                 "reasoning": reasoning, "on_applied": _on_applied,
                                 "snapshot": snapshot, "prev_default": before})
        _persist_session_pin(task_agent, sid, plan.provider, plan.model, reasoning, seat,
                             switched_from=current[1])
        when = "from my next step"
    elif sid:
        _persist_session_pin(task_agent, sid, plan.provider, plan.model, reasoning, seat,
                             switched_from=current[1])
        _emit("model_switched", user_id, sid, attrs)
        when = "from your next message"
    elif not chat_key:
        # No chat is named (the console's Settings picker): only the default moved.
        _emit("model_switched", user_id, "", attrs)
        return (f"✅ Model: {describe(plan)}\nSaved for every new chat. "
                "A chat that is open keeps its model.")
    else:
        if not ok:   # the saved model carries a cold start; an unsaved one rides the queue
            queue_chat_pin(chat_key, {"provider": plan.provider, "model": plan.model,
                                      "reasoning": reasoning})
        _emit("model_switched", user_id, "", attrs)
        when = "when this chat starts (your next message)"
    return f"✅ Model: {describe(plan)}\nApplies {when}. Scope: {scope}."


def _keep_level(sid):
    from agents.task.model_pin_state import session_reasoning
    return session_reasoning(sid)


async def _set_reasoning(task_agent, user_id, sid, agent, cmd, current, seat) -> str:
    from core import model_pins
    from agents.task.model_pin_state import set_session_reasoning
    level = cmd.reasoning
    if not sid:
        if model_pins.owner_default(user_id) is None:
            return ("No live chat and no saved model to keep a level on: write "
                    "/model <name> reasoning=<level>.")
        ok, err = model_pins.set_default_reasoning(user_id, level)
        return ("✅ Reasoning saved for every new chat." if ok else
                f"❌ Reasoning {level}: NOT saved: {err or 'the write failed'}.")
    first = getattr(agent, "_first_model", None) if agent is not None else None
    if first is not None:
        # A provider fallback is temporary: the record keeps the session's own model,
        # never the fallback it returns from after the cooldown.
        current = (first.provider, first.model)
    pending = agent.pending_model_pin() if agent is not None else None
    if pending and pending.get("model"):
        # A queued switch applies its own level at the drain: the new level rides it,
        # and the record keeps the queued model (never the live one it replaces).
        current = (pending.get("provider") or current[0], pending["model"])
    if not await _reasoning_ok(current[0], current[1]):
        return f"❌ {current[1]} has no reasoning setting. Nothing changed."
    refused = _token_level_refused(current[0], level)
    if refused:
        return refused
    set_session_reasoning(sid, level)
    if pending and pending.get("model"):
        pending["reasoning"] = level
    if current[1]:
        _persist_session_pin(task_agent, sid, current[0] or "", current[1], level, seat)
    # The level rides the saved model; with none saved (the operator model) it is this
    # chat's only — said, never reported as saved.
    if model_pins.owner_default(user_id) is None:
        saved = " New chats start on the operator model without it."
    else:
        ok, err = model_pins.set_default_reasoning(user_id, level)
        saved = (" Saved for every new chat." if ok
                 else f" NOT saved for new chats: {err or 'the write failed'}.")
    _emit("model_reasoning_set", user_id, sid, {"level": level, "seat": seat})
    tail = "" if (current[0] or "").lower() in REASONING_PROVIDERS else " It is sent on OpenRouter and DeepSeek only."
    return f"✅ Reasoning {level} for this chat, from my next step.{tail}{saved}"


async def _reset(task_agent, user_id, sid, agent, cmd, seat, chat_key=None) -> str:
    from core import model_pins
    from agents.task.model_pin_state import take_chat_pin
    from core.model_pins import chat_runtime_for
    notes = []
    # The saved model goes too: a reset that kept it would only come back to it.
    saved, unread = model_pins.load(user_id)
    if unread or saved.get("default"):   # an unreadable store is tried, and named if it fails
        ok, err = model_pins.clear_default(user_id)
        notes.append("your saved model cleared" if ok else f"saved model NOT cleared ({err})")
    take_chat_pin(chat_key)
    stale = None if sid else _raw_bound_sid(task_agent, chat_key)
    if stale:   # the chat rolls over next: its old pin must not carry on
        try:
            task_agent.session_manager.update_session_metadata(stale, {"model_pin": None})
        except Exception:
            logger.warning("old model pin not cleared for %s", stale, exc_info=True)
    provider, model, reasoning, source = chat_runtime_for(user_id)
    if not model and provider:
        from modules.llm.llm_client_registry import get_default_model
        model = get_default_model(provider)
    if not (provider and model):
        return "❌ No default model resolves on this box. Nothing changed."
    current = _current(task_agent, sid, agent)
    if agent is not None and current[1] != model:
        snapshot = _switch_snapshot(task_agent, sid, agent)
        agent.request_model_pin({
            "provider": provider, "model": model, "reasoning": reasoning, "snapshot": snapshot,
            "on_applied": _failure_callback(
                task_agent, user_id, sid, snapshot,
                {"to": f"{provider}/{model}", "via": "reset", "seat": seat}, model, current[1])})
    elif agent is not None:
        from agents.task.model_pin_state import set_session_reasoning
        cancel = getattr(agent, "cancel_model_pin", None)
        if callable(cancel):   # a switch queued before the reset must not run after it
            cancel()
        set_session_reasoning(sid, reasoning)
    if sid:
        _persist_session_pin(task_agent, sid, provider, model, reasoning, seat,
                             follows_operator=source.startswith(model_pins.OPERATOR_SOURCE),
                             switched_from=current[1], chat_pin=False)
    _emit("model_switched", user_id, sid or "", {"to": f"{provider}/{model}", "via": "reset",
                                                 "seat": seat})
    head = f"✅ Back to {model} ({provider}) — {source}"
    return head + (f"; {', '.join(notes)}." if notes else ".")


async def _show(task_agent, user_id, sid, agent, chat_key) -> str:
    from agents.task.model_pin_state import peek_chat_pin, session_reasoning
    from core.model_pins import chat_runtime_for
    from core.runtime_config import resolve_default_provider
    lines = []
    if agent is not None or sid:
        provider, model = _current(task_agent, sid, agent)
        pin = (_record(task_agent, sid).get("model_pin") or {}) if sid else {}
        pending = agent.pending_model_pin() if agent is not None else None
        source = ("your saved model" if pin.get("seat") == "start"
                  else "you set it" if pin else "how this chat started")
        price, cached, context, *_ = await _model_facts(provider or "", model or "")
        lines.append(f"**This chat:** `{model or '?'}` ({provider or '?'}) — {source}")
        lines.append(f"Price: {ms.fmt_price(price, cached)}"
                     + (f" · context {context:,}" if context else ""))
        lines.append(f"Reasoning: {session_reasoning(sid) or 'the operator default'}")
        if pending:
            lines.append(f"Queued: {pending.get('model')} ({pending.get('provider')}) — "
                         "applies at my next step")
    else:
        # Only a switch that could not be saved differs from the New chats line below.
        queued = peek_chat_pin(chat_key)
        lines.append("No live chat now." + (f" The next one starts on: {queued['model']} "
                                             f"({queued['provider']})." if queued else ""))
    c_provider, c_model, c_reason, c_source = chat_runtime_for(user_id)
    lines.append(f"**New chats:** `{c_model or 'the provider default'}` ({c_provider}) — {c_source}"
                 + (f", reasoning {c_reason}" if c_reason else ""))
    a_provider, a_model = resolve_default_provider()[:2]
    lines.append(f"**Goals and jobs:** `{a_model or 'the provider default'}` ({a_provider})")
    from agents.task.model_settings import settings_lines   # 121: heavy, light, choice
    lines += settings_lines(user_id)
    # 095 W1c (SC-16): name the lever each form moves (two model levers exist).
    lines += ["", "Switch: `/model <name>` (this chat and every new chat) · reasoning: "
              "`/model reasoning=off|low|medium|high` · back to the operator model: "
              "`/model reset` · names: `/model aliases`"]
    return "\n".join(lines)


#: The built-in names the `/model` picker offers first, when this box has a key for them.
PICKER_ORDER = ("deepseek", "glm", "sonnet", "gpt", "gemini", "kimi", "opus", "haiku",
                "grok", "qwen")
#: Model buttons on the picker.
PICKER_MAX = 6


def picker_buttons(task_agent: Any, *, user_id: str, chat_key: Optional[str] = None,
                   session_id: Optional[str] = None) -> list:
    """The bare ``/model`` buttons (2026-11-20 UX review, H5): the owner's names and
    the built-in names this box has a key for (never the model the chat runs now),
    the reasoning levels, and Reset. A name tap is also the default for new chats. Each is
    the typed line (``core.surfaces.nav``), so a tap meets the same gates."""
    from core import model_pins
    from core.surfaces.nav import NavButton
    sid = _bound_session_id(task_agent, chat_key, session_id)
    live = _live_agent(task_agent, sid)
    provider, model = _current(task_agent, sid, live)
    pending = live.pending_model_pin() if live is not None else None
    if pending and pending.get("model"):   # a queued switch: hide its target, offer the old
        provider, model = pending.get("provider") or provider, pending["model"]
    keyed = {p.lower() for p in _keyed_providers()}
    names = [str(n).lower() for n in (model_pins.aliases(user_id) or {})]
    for n in PICKER_ORDER:
        if n not in names and keyed & set(ms.alias_row(n)):
            names.append(n)
    out = []
    for n in names:
        if len(out) >= PICKER_MAX:
            break
        if model and model in ms.alias_row(n).values():
            continue
        out.append(NavButton(n, f"/model {n}"))
    out.append(NavButton("More names", "/model aliases"))
    if (provider or "").lower() in REASONING_PROVIDERS:
        out += [NavButton(f"Reasoning {lvl}", f"/model reasoning={lvl}")
                for lvl in ("off", "low", "high")]
    out.append(NavButton("Reset", "/model reset"))
    return out


def _notify_owner(task_agent, user_id, text) -> None:
    try:
        from core.async_bridge import spawn_retained
        from core.self_evolution import push_owner_message
        spawn_retained(push_owner_message(getattr(task_agent, "container", None), text,
                                          priority="normal", source="model_switch"))
    except Exception:
        logger.warning("model switch failure not delivered: %s", text)


def resolve_job_model(name: str, user_id: str) -> Tuple[Optional[dict], str]:
    """094: an OWNER-written model pin for a cron job or goal (``polyrob cron edit
    --model``, ``polyrob goals edit --model``): ``({"model", "provider"?}, "")`` or
    ``(None, why)``. Sync (the CLI). The agent has no path here — its job and goal
    tools carry no model field, so unattended spend never moves by a model's choice."""
    cmd = ms.parse_model_args([name, "go"])
    if cmd.action != "set":
        return None, cmd.error or f"'{name}' is not a model name"
    plan, err = asyncio.run(plan_switch(cmd, user_id=user_id))
    if plan is None:
        return None, err.replace(" Nothing changed.", "")
    # Always the provider too: the CLI's env may not be the service's env.
    out = {"model": plan.model, "provider": plan.provider}
    return out, (f"not verified: {plan.verified}" if plan.verified else "")


def with_chat_pin(request_body: dict, user_id: Optional[str]) -> dict:
    """094: an API/console session request with no model or provider starts on the
    owner's default (``model.json``) when this tenant set one; the pin rides along
    as ``_chat_pin`` for :func:`start_with_pin`. A billed request refuses a pin with no
    price and starts on the default; ``_pin_notice`` names the model. Anything else is
    returned as is."""
    body = dict(request_body or {})
    if body.get("model") or body.get("provider"):
        return body
    from agents.task.model_pin_state import stored_pin_refusal
    from core.billing_context import is_billed
    from core.model_pins import chat_pin_for
    pin = chat_pin_for(user_id)
    refusal = stored_pin_refusal(pin["provider"], pin["model"]) if pin and is_billed() else ""
    if refusal:
        logger.warning("model pin refused at load: %s", refusal)
        body["_pin_notice"] = refusal
    elif pin:
        body.update(model=pin["model"], provider=pin["provider"], _chat_pin=pin)
    return body
