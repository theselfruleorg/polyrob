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


def _save_default(ctx, provider, model, reasoning=None, *, tenant=None,
                  validated: bool = True) -> str:
    """098 CL-05: the default is written AFTER the live swap succeeded, through
    the one owner of the three stores; each store's result is said."""
    from cli.config_store import env_default_override_note
    from cli.model_default import persist_default
    res = persist_default(provider, model, reasoning, tenant=tenant)
    note = env_default_override_note(provider) or ""
    tail = "" if validated else " (not validated by a live swap)"
    return f" {res.line()}{tail}.{(' ' + note) if note else ''}"


async def handle(ctx, *, pick_fn) -> None:
    from cli.config_store import resolve_model_alias, _provider_for_model
    from agents.task.model_switch_service import describe, plan_switch
    args = list(ctx.args or [])
    user_id = _tenant(ctx)
    if not args:
        picked = await pick_fn(_current(ctx))
        if not picked:
            ctx.emit("Cancelled.")
            return
        provider, model = picked
        if ctx.agent is None:
            ctx.emit(f"No live session.{_save_default(ctx, provider, model, validated=False)}")
            return
        if await swap_live(ctx, provider, model, saved_note="The default did not change."):
            ctx.emit(f"Model swapped live: {model} ({provider}).{_save_default(ctx, provider, model)}")
        return
    cmd = ms.parse_model_args(args)
    if cmd.action == "error":
        ctx.emit(cmd.error)
        return
    if cmd.action == "choice" or cmd.tier:   # 121: an owner setting — the chat seats' own path
        from agents.task.model_settings import settings_command
        ctx.emit(await settings_command(cmd, user_id, "repl"))
        return
    if cmd.action in ("aliases", "alias", "reasoning", "reset", "show"):
        await _via_service(ctx, cmd, args, user_id)
        return
    agent = ctx.agent
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
    if plan.default and agent is None:
        ctx.emit("No live session." + _save_default(
            ctx, plan.provider, plan.model, plan.reasoning, tenant=user_id, validated=False))
        return
    if await swap_live(ctx, plan.provider, plan.model, plan.reasoning,
                       saved_note="The default did not change." if plan.default else ""):
        saved = (_save_default(ctx, plan.provider, plan.model, plan.reasoning, tenant=user_id)
                 if plan.default else "")
        ctx.emit(f"Model swapped live: {describe(plan)}.{saved}")


async def _via_service(ctx, cmd, args, user_id: str) -> None:
    """aliases / alias / reasoning / reset / show: the ONE service the chat seats call
    (``model_switch_service.model_command``). The REPL keeps only the glue: a switch the
    service queues is applied NOW (the REPL runs between turns), and ``reset default``
    also clears the REPL's own ``cli.json`` default."""
    from agents.task.model_switch_service import model_command
    words = list(args)
    if cmd.action == "reset" and (cmd.default or any(w.lower() == "default" for w in words)):
        from cli.model_default import clear_default
        ctx.emit(clear_default(tenant=user_id).line("cleared"))
        words = [w for w in words if w.lower() != "default"]
    agent = ctx.agent
    reply = await model_command(getattr(ctx, "task_agent", None), user_id=user_id,
                                words=words, session_id=getattr(ctx, "session_id", "") or
                                getattr(agent, "session_id", None), seat="repl", agent=agent)
    ctx.emit(reply)
    pending = getattr(agent, "pending_model_pin", None)
    if not (callable(pending) and pending()):
        return
    res = await agent.apply_pending_model_pin() or {}
    if not res.get("ok"):
        ctx.emit(f"Live swap failed ({res.get('error')}); the session keeps its model.")
        return
    if ctx.state is not None:
        ctx.state.model = res.get("model") or ctx.state.model
        ctx.state.provider = res.get("provider") or ctx.state.provider


def current_line(ctx) -> Optional[str]:
    provider, model = _current(ctx)
    return f"{model} ({provider})" if model else None
