"""094: the ONE ``/model`` pipeline every owner seat calls — parse, resolve,
validate, guard, then pin and swap at the next step boundary.

* parse + aliases + guard rule: ``core/model_switch.py`` (pure);
* the owner's default + aliases: ``core/model_pins.py``;
* the live OpenRouter list (exists? tools? price? window?): ``modules/llm/openrouter_catalog.py``;
* the swap: ``Agent.request_model_pin`` → applied in the user-message drain
  (``agents/task/agent/core/user_ingress.py``) by ``swap_model``.

Scopes: ``/model <name>`` pins THIS chat's session (its record's
``request.model/provider`` + ``model_pin``; survives a restart, ends at ``/new``);
``default`` also writes the owner's default for every new owner chat.
The operator env (``DEFAULT_MODEL``) stays the floor and is never written here,
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
    quote: str = ""         # non-empty = the price guard asks for `go`


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
    if not cmd.confirm and current[1] and current[1] != res.model:
        cur_price = (await _model_facts(current[0] or res.provider, current[1]))[0]
        why = ms.guard_reason(cur_price, price)
        if why:
            line = " ".join(["/model"] + [w for w in cmd.words if w.lower() not in ("go",)] + ["go"])
            plan.quote = (f"Switch to {res.model} ({res.provider})? {why.capitalize()}.\n"
                          f"Price: {ms.fmt_price(price, cached)}"
                          f"{f' · context {context:,}' if context else ''}.\n"
                          "The first message after the switch re-reads this chat once at the "
                          "new price (the prompt cache belongs to the old model).\n"
                          f"To switch: {line}")
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
                     + ("" if plan.provider == "openrouter" else " (sent on OpenRouter only)"))
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


def _failure_callback(task_agent, user_id, sid, snapshot, attrs, target, current_model):
    """``on_applied`` for a queued swap: success records it; a failed build puts the
    session record back EXACTLY as it was and tells the owner."""
    async def _on_applied(result: dict) -> None:
        if result.get("ok"):
            _emit("model_switched", user_id, sid, attrs)
            return
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
                         switched_from: Optional[str] = None) -> None:
    """The session record carries the pin: a recreate builds on it, B-4 keeps it.
    ``follows_operator`` (a reset onto ``DEFAULT_MODEL``) stamps ``operator_pin`` so the
    session follows the next operator change again (B-4). ``switched_from`` + its
    time ride the pin (095 W1a) so a reload still says who switched the model."""
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
            "model_pin": None if follows_operator else pin})
    except Exception:
        logger.warning("model pin not persisted for %s", session_id, exc_info=True)


def _emit(kind: str, user_id: str, session_id: str, attrs: dict) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="model_switch", user_id=user_id, session_id=session_id or "",
             attrs=attrs)
    except Exception:
        pass


def owner_chat_pin(chat_key: Optional[str], user_id: str) -> Optional[dict]:
    """094: the pin a NEW owner chat starts on — a switch queued for this chat
    while it had no session, else the owner's default — or None (operator pin)."""
    from agents.task.model_pin_state import take_chat_pin
    from core.model_pins import chat_pin_for
    return take_chat_pin(chat_key) or chat_pin_for(user_id)


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
                        session_id: Optional[str] = None, seat: str = "telegram") -> str:
    """``/model …`` on a chat seat (Telegram, console). Returns the reply text."""
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
    sid = _bound_session_id(task_agent, chat_key, session_id)
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
    if plan.quote:
        return plan.quote
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
    scope = "this chat until /new"
    if plan.default:
        ok, err = model_pins.set_default(user_id, plan.provider, plan.model, plan.reasoning)
        if not ok:
            return f"❌ Default not saved ({err}). Nothing changed."
        scope += " · also your default for every new chat"
    attrs = {"from": f"{current[0] or '?'}/{current[1] or '?'}",
             "to": f"{plan.provider}/{plan.model}", "via": plan.via, "seat": seat,
             "default": plan.default, "reasoning": plan.reasoning or ""}
    if agent is not None:
        reasoning = plan.reasoning if plan.reasoning is not None else (
            _keep_level(sid) if plan.reasoning_ok else None)

        snapshot = _switch_snapshot(task_agent, sid, agent)
        _on_applied = _failure_callback(task_agent, user_id, sid, snapshot,
                                        attrs, plan.model, current[1])
        agent.request_model_pin({"provider": plan.provider, "model": plan.model,
                                 "reasoning": reasoning, "on_applied": _on_applied,
                                 "snapshot": snapshot})
        _persist_session_pin(task_agent, sid, plan.provider, plan.model, reasoning, seat,
                             switched_from=current[1])
        when = "from my next step"
    elif sid:
        _persist_session_pin(task_agent, sid, plan.provider, plan.model, plan.reasoning, seat,
                             switched_from=current[1])
        _emit("model_switched", user_id, sid, attrs)
        when = "from your next message"
    else:
        queue_chat_pin(chat_key, {"provider": plan.provider, "model": plan.model,
                                  "reasoning": plan.reasoning})
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
        if cmd.default:
            ok, err = model_pins.set_default_reasoning(user_id, level)
            return "✅ Default reasoning saved." if ok else f"❌ {err}"
        return "No live chat: write /model <name> reasoning=<level> to start one with it."
    pending = agent.pending_model_pin() if agent is not None else None
    if pending and pending.get("model"):
        # A queued switch applies its own level at the drain: the new level rides it,
        # and the record keeps the queued model (never the live one it replaces).
        current = (pending.get("provider") or current[0], pending["model"])
    if not await _reasoning_ok(current[0], current[1]):
        return f"❌ {current[1]} has no reasoning setting. Nothing changed."
    set_session_reasoning(sid, level)
    if pending and pending.get("model"):
        pending["reasoning"] = level
    if current[1]:
        _persist_session_pin(task_agent, sid, current[0] or "", current[1], level, seat)
    if cmd.default:
        model_pins.set_default_reasoning(user_id, level)
    _emit("model_reasoning_set", user_id, sid, {"level": level, "seat": seat})
    tail = "" if (current[0] or "").lower() == "openrouter" else " It is sent on OpenRouter only."
    return f"✅ Reasoning {level} for this chat, from my next step.{tail}"


async def _reset(task_agent, user_id, sid, agent, cmd, seat, chat_key=None) -> str:
    from core import model_pins
    from agents.task.model_pin_state import take_chat_pin
    from core.model_pins import chat_runtime_for
    notes = []
    if cmd.default or "default" in [w.lower() for w in cmd.words]:
        ok, err = model_pins.clear_default(user_id)
        notes.append("your default cleared" if ok else f"default not cleared ({err})")
    take_chat_pin(chat_key)
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
                             follows_operator="DEFAULT_MODEL" in source,
                             switched_from=current[1])
    _emit("model_switched", user_id, sid or "", {"to": f"{provider}/{model}", "via": "reset",
                                                 "seat": seat})
    head = f"✅ Back to {model} ({provider}) — {source}"
    return head + (f"; {', '.join(notes)}." if notes else ".")


async def _show(task_agent, user_id, sid, agent, chat_key) -> str:
    from agents.task.model_pin_state import peek_chat_pin, session_reasoning
    from core.model_pins import chat_runtime_for
    from core.runtime_config import heavy_model_pin, resolve_default_provider
    lines = []
    if agent is not None or sid:
        provider, model = _current(task_agent, sid, agent)
        pin = (_record(task_agent, sid).get("model_pin") or {}) if sid else {}
        pending = agent.pending_model_pin() if agent is not None else None
        source = (f"this chat, set from {pin.get('seat', 'a seat')}" if pin
                  else "how this chat started")
        price, cached, context, *_ = await _model_facts(provider or "", model or "")
        lines.append(f"Model: {model or '?'} ({provider or '?'}) — {source}")
        lines.append(f"Price: {ms.fmt_price(price, cached)}"
                     + (f" · context {context:,}" if context else ""))
        lines.append(f"Reasoning: {session_reasoning(sid) or 'operator env (OPENROUTER_REASONING_*)'}")
        if pending:
            lines.append(f"Queued: {pending.get('model')} ({pending.get('provider')}) — "
                         "applies at my next step")
    else:
        queued = peek_chat_pin(chat_key)
        lines.append("No live chat now." + (f" Queued for it: {queued['model']} "
                                             f"({queued['provider']})." if queued else ""))
    c_provider, c_model, c_reason, c_source = chat_runtime_for(user_id)
    lines.append(f"New chats start on: {c_model or 'the provider default'} ({c_provider}) — {c_source}"
                 + (f", reasoning {c_reason}" if c_reason else ""))
    a_provider, a_model = resolve_default_provider()[:2]
    heavy = heavy_model_pin()
    lines.append(f"Autonomous runs: {a_model or 'the provider default'} ({a_provider})"
                 f"; money-class: {heavy or 'HEAVY_MODEL unset'}")
    # 095 W1c (SC-16): name the lever each form moves (two model levers exist).
    lines.append("Change: /model <name> (this chat) · /model <name> default (your default for "
                 "new chats; the operator's DEFAULT_MODEL stays the floor) · /model <name> "
                 "reasoning=off|on|low|medium|high · /model reset · /model aliases")
    return "\n".join(lines)


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


async def child_model_refusal(parent_agent: Any, model: str, provider: Optional[str]) -> str:
    """094: ``delegate_task(model=…)`` is the one place the AGENT picks a model.
    It may pick: its own model, a model the owner already uses (``DEFAULT_MODEL``,
    ``HEAVY_MODEL``, the owner's chat default or an owner alias), or one that costs
    no more than its own (input AND output). Anything else is refused — a prompt
    injection must not point a 150-step child at the priciest model. ``""`` = allowed."""
    from core import model_pins
    from core.model_pins import chat_pin_for
    from core.runtime_config import heavy_model_pin, operator_model_pin
    own = getattr(parent_agent, "model_name", None)
    owner = getattr(parent_agent, "user_id", None)
    allowed = {own, operator_model_pin(), heavy_model_pin()}
    pin = chat_pin_for(owner)
    if pin:
        allowed.add(pin.get("model"))
    for target in model_pins.aliases(owner).values():
        allowed.add(target.split("/", 1)[1] if target.split("/", 1)[0] in _known_providers()
                    else target)
    if model in allowed:
        return ""
    own_provider = getattr(parent_agent, "llm_provider", None) or getattr(parent_agent, "provider_name", "")
    mine = (await _model_facts(own_provider or "", own or ""))[0]
    theirs = (await _model_facts(provider or own_provider or "", model))[0]
    if mine and theirs and theirs[0] <= mine[0] and theirs[1] <= mine[1]:
        return ""
    return (f"refused: a sub-agent may run on your model ({own}), a cheaper one, or a model "
            f"the owner uses or listed (/model alias). '{model}' is none of these"
            + ("" if theirs else " (its price is unknown)") + ".")


def with_chat_pin(request_body: dict, user_id: Optional[str]) -> dict:
    """094: an API/console session request with no model or provider starts on the
    owner's default (``model.json``) when this tenant set one; the pin rides along
    as ``_chat_pin`` for :func:`start_with_pin`. Anything else is returned as is."""
    body = dict(request_body or {})
    if body.get("model") or body.get("provider"):
        return body
    from core.model_pins import chat_pin_for
    pin = chat_pin_for(user_id)
    if pin:
        body.update(model=pin["model"], provider=pin["provider"], _chat_pin=pin)
    return body
