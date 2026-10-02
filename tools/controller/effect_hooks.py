"""Controller hooks that classify, gate and record every outward act (proposal 033).

Two hooks, both registered in ``Controller.__init__`` — never post-hoc on an
instance, because a delegated sub-agent builds a fresh ``Controller`` and a fresh
``HookPipeline`` (``agents/task/agent/sub_agent_manager.py``), so a hook attached
to a parent would never cover delegated work.

- **gate** (pre, ``fail_mode="closed"``): an autonomous or forged turn may not
  make a ``social`` / ``comms`` / ``public`` / ``money`` write while the owner's
  pause covers it. An OWNER-initiated turn is never gated — asking the agent to
  post IS the owner being in the loop; the same polarity every money gate uses.
  A refusal is recorded as ``outcome="denied"``, so a blocked write is more
  visible than an allowed one. Under ``EXTERNAL_WRITE_STRICT`` a write verb with
  no explicit classification row is refused as well.
- **record** (post, ``fail_mode="open"``): one ``external_write`` row per
  outward act, on success AND on the error/timeout path
  (``Controller._observe_error_result``), which is why ``outcome`` is real.

Turn origin cannot be computed in ``core/`` (the autonomy marker lives in
``agents/``), so it is resolved HERE and handed to the tier-0 recorder as data.

NOTE: no ``from __future__ import annotations`` in this package — it stringizes
the registry's first-param annotations.
"""
import logging

logger = logging.getLogger(__name__)

#: The parameter names that carry the content or the addressee of a write, in
#: the order the dedup fingerprint prefers them. Only a HASH ever leaves here.
_FINGERPRINT_KEYS = ("text", "body", "content", "message", "tweet", "to",
                     "target", "slug", "url", "code", "command")


def resolve_action_tool(controller, action_name):
    """The owning tool_id, or None for a directly-registered closure. The same
    resolver the correspondent gate's Layer 2 uses — the only way to classify a
    dynamically named MCP action."""
    try:
        details = controller.get_action_details(action_name)
        return getattr(details, "tool", None) if details is not None else None
    except Exception:
        return None


def _mcp_read_only(controller, action_name):
    """A server-declared ``readOnlyHint`` stamped on the MCP executor. Narrowing
    only: absent or unreadable reads False (the ``network`` ceiling stands)."""
    try:
        details = controller.get_action_details(action_name)
        fn = getattr(details, "function", None)
        return bool(getattr(fn, "_mcp_read_only", False))
    except Exception:
        return False


def _classify(controller, action_name):
    from core.effects import classify_effect
    tool_id = resolve_action_tool(controller, action_name)
    ro = _mcp_read_only(controller, action_name) if tool_id == "mcp" else False
    return tool_id, classify_effect(tool_id, action_name, mcp_read_only=ro)


def _autonomous(execution_context, controller):
    """Turn origin. A context-less call is a programmatic/owner-direct call (the
    ``message_pause_refusal`` polarity); an unprovable turn otherwise reads
    autonomous — the gate must fail toward the owner's pause."""
    if execution_context is None:
        return False
    try:
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        return bool(_is_forged_or_autonomous_turn(execution_context, controller))
    except Exception:
        return True


def _surface(execution_context):
    try:
        meta = getattr(execution_context, "metadata", None) or {}
        kind = str(meta.get("surface") or meta.get("turn_kind") or "")
    except Exception:
        kind = ""
    return kind or "agent"


def _params_dict(params):
    if isinstance(params, dict):
        return params
    try:
        return params.model_dump()  # a pydantic param model
    except Exception:
        return {}


def _target(params):
    """The EXISTING outbound tier vocabulary where the params say it outright;
    ``unknown`` otherwise. Never guessed — the tool's own gate owns the real tier."""
    p = _params_dict(params)
    raw = str(p.get("target") or p.get("to") or "").strip().lower()
    if raw == "owner":
        return "owner"
    return "unknown"


def _fingerprint(params):
    p = _params_dict(params)
    for key in _FINGERPRINT_KEYS:
        v = p.get(key)
        if isinstance(v, str) and v:
            return v[:400]
    return ""


def _outcome(result):
    if result is None:
        return "ok"
    return "error" if getattr(result, "error", None) else "ok"


def _record(controller, action_name, params, execution_context, tool_id, verdict, outcome):
    from core.effects import record_external_write
    record_external_write(
        effect=verdict.effect, tool=tool_id or "", action=action_name,
        target=_target(params), surface=_surface(execution_context),
        autonomous=_autonomous(execution_context, controller),
        confidence=verdict.confidence, outcome=outcome,
        user_id=str(getattr(execution_context, "user_id", "") or ""),
        session_id=str(getattr(execution_context, "session_id", "") or ""),
        fingerprint=_fingerprint(params))


def make_effect_record_hook(controller):
    """post_tool_call: record one ``external_write`` row per outward act.

    Register with ``fail_mode="open"`` — telemetry must never break execution.
    """
    async def _effect_record_hook(action_name, params, result, execution_context):
        try:
            tool_id, verdict = _classify(controller, action_name)
            if verdict is None:
                return
            _record(controller, action_name, params, execution_context,
                    tool_id, verdict, _outcome(result))
        except Exception:
            logger.debug("effect record hook failed (fail-open)", exc_info=True)

    return _effect_record_hook


def make_effect_gate_hook(controller):
    """pre_tool_call: the effect-derived pause gate (and the strict-mode refusal).

    Register with ``fail_mode="closed"`` — a guardrail that crashes must DENY.
    Returns a refusal string, or None to allow.
    """
    async def _effect_gate_hook(action_name, params, execution_context):
        from core.effects import (effect_pause_refusal,
                                  external_write_pause_gate_enabled,
                                  external_write_strict)
        tool_id, verdict = _classify(controller, action_name)
        if verdict is None:
            return None
        reason = None
        if (external_write_strict() and verdict.confidence == "tool"
                and tool_id != "mcp"):
            # MCP actions are named by a third-party server at runtime; they can
            # never carry an explicit row, so strict mode cannot mean "refuse".
            reason = (f"'{action_name}' is an unclassified {verdict.effect} write "
                      "(EXTERNAL_WRITE_STRICT). Add it to core/effects.py "
                      "WRITE_ACTIONS or NON_WRITE_ACTIONS.")
        elif (external_write_pause_gate_enabled()
                and _autonomous(execution_context, controller)):
            reason = effect_pause_refusal(verdict.effect, tool_id, action_name)
        if reason:
            try:
                _record(controller, action_name, params, execution_context,
                        tool_id, verdict, "denied")
            except Exception:
                logger.debug("effect denial record failed (fail-open)", exc_info=True)
        return reason

    return _effect_gate_hook


__all__ = ["make_effect_gate_hook", "make_effect_record_hook", "resolve_action_tool"]
