"""094: the REPL ``/model`` — the same grammar and pipeline as every chat seat
(``core/model_switch.py``, ``agents/task/model_switch_service.py``).

The REPL holds its live agent and runs only between turns (``/model`` is not a
live command), so a switch is applied NOW instead of queued. Its own new-session
default stays ``cli.json`` (``default`` writes it AND the owner's default for the
chat seats); the bare ``/model`` keeps the interactive picker.
"""
from __future__ import annotations

from typing import Any, Optional

from core import model_switch as ms


def _tenant(ctx) -> str:
    from cli._admin_home import admin_owner_tenant
    return admin_owner_tenant(getattr(ctx, "user_id", None))


def _current(ctx) -> tuple:
    agent = ctx.agent
    if agent is None:
        return None, None
    return (getattr(agent, "llm_provider", None) or getattr(agent, "provider_name", None),
            getattr(agent, "model_name", None))


async def swap_live(ctx, provider: str, model: str, reasoning: Any = None,
                    *, saved_note: str = "") -> bool:
    """Swap the REPL's live agent; repaint the frame. True on success."""
    agent = ctx.agent
    if agent is None or not hasattr(agent, "swap_model"):
        ctx.emit(f"No live session to swap. {saved_note}".strip())
        return False
    kw = {"cause": "owner"} if reasoning is None else {"reasoning": reasoning, "cause": "owner"}
    res = await agent.swap_model(provider, model, **kw)
    if not res.get("ok"):
        ctx.emit(f"Live swap failed ({res.get('error')}); the session keeps its model. "
                 f"{saved_note}".strip())
        return False
    if ctx.state is not None:
        ctx.state.model = res.get("model") or model
        ctx.state.provider = res.get("provider") or provider
    return True


async def handle(ctx, *, pick_fn) -> None:
    from cli.config_store import (env_default_override_note, resolve_model_alias,
                                  set_default_model, _provider_for_model)
    from core import model_pins
    from agents.task.model_switch_service import describe, plan_switch
    args = list(ctx.args or [])
    user_id = _tenant(ctx)
    if not args:
        picked = await pick_fn(_current(ctx))
        if not picked:
            ctx.emit("Cancelled.")
            return
        provider, model = picked
        set_default_model(provider, model)
        note = env_default_override_note(provider) or ""
        if await swap_live(ctx, provider, model, saved_note=f"Saved as the default for new sessions. {note}"):
            ctx.emit(f"Model swapped live: {model} ({provider}). Also saved as the default "
                     f"for new sessions.{(' ' + note) if note else ''}")
        return
    cmd = ms.parse_model_args(args)
    if cmd.action == "error":
        ctx.emit(cmd.error)
        return
    if cmd.action == "aliases":
        table, err = model_pins.aliases_read(user_id)
        ctx.emit("\n".join(ms.alias_lines(table, read_error=err)))
        return
    if cmd.action == "alias":
        target = None if cmd.alias_target == "-" else cmd.alias_target
        ok, err = model_pins.set_alias(user_id, cmd.name, target)
        ctx.emit((f"Alias {cmd.name} → {target}." if target else f"Alias {cmd.name} removed.")
                 if ok else f"Alias not saved: {err}")
        return
    agent = ctx.agent
    if cmd.action == "reasoning":
        from agents.task.model_pin_state import set_session_reasoning
        if agent is None:
            ctx.emit("No live session.")
            return
        set_session_reasoning(getattr(agent, "session_id", None), cmd.reasoning)
        if cmd.default:
            model_pins.set_default_reasoning(user_id, cmd.reasoning)
        ctx.emit(f"Reasoning {cmd.reasoning} for this session (sent on OpenRouter).")
        return
    if cmd.action == "reset":
        from cli.config_store import get_default_model
        if cmd.default or any(w.lower() == "default" for w in cmd.words):
            model_pins.clear_default(user_id)
        provider, model = get_default_model()
        if not (provider and model):
            from core.runtime_config import resolve_default_provider
            from modules.llm.llm_client_registry import get_default_model as reg_default
            provider, model = resolve_default_provider()[:2]
            model = model or (reg_default(provider) if provider else None)
        if provider and model and await swap_live(ctx, provider, model):
            ctx.emit(f"Back to {model} ({provider}).")
        return
    # set — a cli.json alias (B6) is the REPL's own short name; it wins locally.
    if not cmd.provider and "/" not in cmd.name:
        legacy = resolve_model_alias(cmd.name)
        if legacy and legacy[1]:
            cmd.provider = legacy[0] or _provider_for_model(legacy[1]) or ""
            cmd.name = legacy[1]
    plan, err = await plan_switch(cmd, user_id=user_id, current=_current(ctx))
    if plan is None:
        ctx.emit(err)
        return
    if plan.quote:
        ctx.emit(plan.quote)
        return
    saved = ""
    if plan.default:
        set_default_model(plan.provider, plan.model)
        ok, why = model_pins.set_default(user_id, plan.provider, plan.model, plan.reasoning)
        saved = (" Also your default for new sessions and chats." if ok
                 else f" Default not saved: {why}.")
        note = env_default_override_note(plan.provider)
        saved += f" {note}" if note else ""
    if await swap_live(ctx, plan.provider, plan.model, plan.reasoning):
        ctx.emit(f"Model swapped live: {describe(plan)}.{saved}")


def current_line(ctx) -> Optional[str]:
    provider, model = _current(ctx)
    return f"{model} ({provider})" if model else None
