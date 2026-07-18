"""Built-in extension-event specs registered on the D2 registry (T4-02).

First real consumer of ``cli/ui/event_registry.py``: renders the memory
observability events (`memory_recall` / `memory_write`) when they arrive on the
session feed. Registration happens at import (events.py imports this module),
one ``register_event`` call per type — the whole point of the D2 seam.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from cli.ui.event_registry import EventSpec, Layer, RegisteredEvent, register_event


def _parse(feed_dict: Dict[str, Any]) -> RegisteredEvent:
    return RegisteredEvent(
        type=feed_dict.get("type", "unknown"),
        data=feed_dict.get("data", {}) or {},
        raw=feed_dict,
    )


def _render_memory(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    verb = "recalled" if event.type == "memory_recall" else "wrote"
    scope = str(d.get("scope") or "memory")
    chars = d.get("chars")
    size = f", {chars} chars" if isinstance(chars, int) and chars else ""
    preview = str(d.get("preview") or "").strip()
    tail = f" — {preview}" if preview else ""
    return f"memory {verb} ({scope}{size}){tail}"


def register_builtin_extension_events() -> None:
    """Idempotent: register_event replaces an existing spec for the same type."""
    for kind in ("memory_recall", "memory_write"):
        register_event(EventSpec(
            type=kind,
            parse=_parse,
            layer=Layer.TRACE,  # scaffolding detail: visible under /verbose
            render_line=_render_memory,
        ))
    _register_run_state_events()


# ---------------------------------------------------------------------------
# 019 P1 run-state events: compaction / retry / sub-agent / delegation /
# provider failover. Bar-visible states use ``apply`` (SessionState's
# current-activity fields); scrollback lines by layer (TRACE = verbose-only
# scaffolding, DIALOG = always visible).
# ---------------------------------------------------------------------------


def _apply_compaction_started(state, event: RegisteredEvent) -> None:
    mode = str(event.data.get("mode") or "")
    label = f"compacting ({mode})" if mode else "compacting"
    state._set_activity("compacting", label)


def _apply_compaction_finished(state, event: RegisteredEvent) -> None:
    state._clear_activity("compacting")


def _render_compaction(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    mode = d.get("mode") or "?"
    if event.type == "compaction_started":
        return f"[compact] start mode={mode} tokens={d.get('tokens_before')}"
    ok = d.get("success", True)
    return (f"[compact] done mode={mode} tokens={d.get('tokens_before')}"
            f"→{d.get('tokens_after')} dur={d.get('duration_seconds', 0):.1f}s"
            + ("" if ok else " (no-op)"))


def _apply_retry_wait(state, event: RegisteredEvent) -> None:
    d = event.data or {}
    reason = str(d.get("reason") or "retry")
    delay = d.get("delay_sec")
    label = f"retry ({reason})"
    if isinstance(delay, (int, float)) and delay:
        label += f" {delay:.0f}s"
    state._set_activity("retrying", label)


def _render_retry_wait(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    provider = f" provider={d['provider']}" if d.get("provider") else ""
    return (f"[retry] {d.get('reason', '?')} wait={d.get('delay_sec', 0):.0f}s"
            f" attempt={d.get('attempt', '?')}{provider}")


def _apply_subagent_started(state, event: RegisteredEvent) -> None:
    goal = str(event.data.get("goal_preview") or "")[:40]
    state._set_activity("delegating", f"sub-agent: {goal}" if goal else "sub-agent")


def _apply_subagent_finished(state, event: RegisteredEvent) -> None:
    state._clear_activity("delegating")


def _render_subagent(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    goal = str(d.get("goal_preview") or "")[:80]
    if event.type == "subagent_started":
        return f"+ sub-agent started: {goal}" if goal else "+ sub-agent started"
    ok = "✓" if d.get("ok") else "✗"
    dur = d.get("duration_seconds")
    tail = f" · {dur:.0f}s" if isinstance(dur, (int, float)) and dur else ""
    return f"+ sub-agent finished {ok}{tail}"


def _render_delegation(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    did = d.get("delegation_id") or "?"
    goal = str(d.get("goal_preview") or "")[:80]
    if event.type == "delegation_dispatched":
        return f"⇢ background delegation {did} dispatched: {goal}"
    dur = d.get("duration_seconds")
    tail = f" · {dur:.0f}s" if isinstance(dur, (int, float)) and dur else ""
    return f"⇢ background delegation {did} {d.get('status') or 'done'}{tail}"


def _render_provider_failure(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    line = (f"provider {d.get('failed_provider') or '?'} failed"
            f" ({d.get('error_type') or 'error'})")
    if d.get("fallback_provider"):
        line += f" — trying {d['fallback_provider']}"
    return line


def _render_provider_fallback(event: RegisteredEvent) -> Optional[str]:
    d = event.data or {}
    return (f"provider fallback: {d.get('original_provider') or '?'}"
            f" → {d.get('fallback_provider') or '?'} ✓")


def _register_run_state_events() -> None:
    specs = (
        EventSpec(type="compaction_started", parse=_parse, layer=Layer.TRACE,
                  apply=_apply_compaction_started, render_line=_render_compaction),
        EventSpec(type="compaction_finished", parse=_parse, layer=Layer.TRACE,
                  apply=_apply_compaction_finished, render_line=_render_compaction),
        EventSpec(type="retry_wait", parse=_parse, layer=Layer.TRACE,
                  apply=_apply_retry_wait, render_line=_render_retry_wait),
        EventSpec(type="subagent_started", parse=_parse, layer=Layer.DIALOG,
                  apply=_apply_subagent_started, render_line=_render_subagent),
        EventSpec(type="subagent_finished", parse=_parse, layer=Layer.DIALOG,
                  apply=_apply_subagent_finished, render_line=_render_subagent),
        EventSpec(type="delegation_dispatched", parse=_parse, layer=Layer.DIALOG,
                  render_line=_render_delegation),
        EventSpec(type="delegation_completed", parse=_parse, layer=Layer.DIALOG,
                  render_line=_render_delegation),
        EventSpec(type="provider_failure", parse=_parse, layer=Layer.DIALOG,
                  render_line=_render_provider_failure),
        EventSpec(type="provider_fallback_success", parse=_parse, layer=Layer.DIALOG,
                  render_line=_render_provider_fallback),
    )
    for spec in specs:
        register_event(spec)


register_builtin_extension_events()
