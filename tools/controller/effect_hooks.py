"""Controller hooks that classify, gate and record every outward act (proposal 033).

Three hooks, all registered in ``Controller.__init__`` — never post-hoc on an
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
- **repeat** (pre, ``fail_mode="open"``): a verb that declares a subject
  (``VerbPolicy.subject``) acts once per subject in the window. The refusal is a
  fact with no ask; the owner typing passes. A verb with no subject never reads
  the log, so money stays with ``tx_guard``.
- **record** (post, ``fail_mode="open"``): one ``external_write`` row per
  outward act, on success AND on the error/timeout path
  (``Controller._observe_error_result``), which is why ``outcome`` is real.

Turn origin cannot be computed in ``core/`` (the autonomy marker lives in
``agents/``), so it is resolved HERE and handed to the tier-0 recorder as data.

NOTE: no ``from __future__ import annotations`` in this package — it stringizes
the registry's first-param annotations.
"""
import asyncio
import json
import logging
import re
import time
import types

logger = logging.getLogger(__name__)


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
    from core.effects import EffectVerdict, classify_effect
    tool_id = resolve_action_tool(controller, action_name)
    if tool_id == "mcp":
        # 085 A5: an action of an owner-marked money server is a money write,
        # so the owner's `/pause` stops it (core.money.classify is the ONE answer).
        from core.money.classify import money_action
        if money_action(action_name):
            return tool_id, EffectVerdict("money", "action")
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


def _declared_subject(action_name):
    """The subject field a verb declares (``VerbPolicy.subject``), or ``""``."""
    from core.verb_policy import VERB_POLICY
    row = VERB_POLICY.get(str(action_name or ""))
    return row.subject if row is not None else ""


def _subject(controller, action_name, params):
    """What this act answers: the declared field, read from the params, else through the
    verb's own param model, so a property such as ``XReplyAction.status_id`` normalizes the
    value. ``""`` for a verb with no subject, or when the params do not name one."""
    field = _declared_subject(action_name)
    if not field:
        return ""
    value = params.get(field) if isinstance(params, dict) else getattr(params, field, None)
    if value is None and isinstance(params, dict):
        details = controller.get_action_details(action_name)
        model = getattr(details, "param_model", None)
        value = getattr(model(**params), field, None) if model is not None else None
    return str(value or "").strip()


def _fingerprint(params):
    """Every param that holds a value, so two rows share a ``dedup`` only when the calls
    match: the same text to two addressees differs, and a verb's own param names
    (``texts``, ``tweet_id``, a swap's amounts) count. Only a HASH ever leaves here."""
    p = {k: v for k, v in _params_dict(params).items() if v not in (None, "", [], {})}
    return json.dumps(p, sort_keys=True, default=str) if p else ""


def _lists_only(action_name, params):
    """``message(action="posts")`` reads the post ledger. It sends nothing, so it is no act."""
    if action_name != "message":
        return False
    return str(_params_dict(params).get("action") or "").strip().lower() == "posts"


def _outcome(result):
    if result is None:
        return "ok"
    return "error" if getattr(result, "error", None) else "ok"


#: 103: what the action produced: a tx hash, or the id a surface gave. The act's content
#: is never stored; these are references to quote and to check.
_REF_PATTERNS = (re.compile(r"\b0x[0-9a-fA-F]{64}\b"),
                 re.compile(r"\b(?:tweet_id|post_id|message_id)\W{1,4}([\w-]{4,64})"))
#: A public post's receipt names its new id in words or in a permalink. Social rows only: a
#: ``message`` receipt quotes the sent text, which may hold a link to anybody's post.
_SOCIAL_REF_PATTERNS = (re.compile(r"/status/(\d{4,})"),
                        re.compile(r"\btweet (\d{4,})"),
                        re.compile(r"\bthread: (\d{4,})"))


def _result_ref(result, effect=""):
    text = str(getattr(result, "extracted_content", "") or "")[:4000]
    patterns = _REF_PATTERNS + (_SOCIAL_REF_PATTERNS if effect == "social" else ())
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(match.lastindex or 0)
    return ""


def _record(controller, action_name, params, execution_context, tool_id, verdict, outcome,
            result=None):
    from core.effects import record_external_write
    ref = _result_ref(result, verdict.effect)
    try:
        subject = _subject(controller, action_name, params)
    except Exception:
        logger.warning("effect record: no subject for %s", action_name, exc_info=True)
        subject = ""
    record_external_write(
        effect=verdict.effect, tool=tool_id or "", action=action_name,
        target=_target(params), surface=_surface(execution_context),
        autonomous=_autonomous(execution_context, controller),
        confidence=verdict.confidence, outcome=outcome,
        user_id=str(getattr(execution_context, "user_id", "") or ""),
        session_id=str(getattr(execution_context, "session_id", "") or ""),
        fingerprint=_fingerprint(params), subject=subject,
        extra={"ref": ref} if ref else None)


def record_approved_send(action_name, params, *, user_id, ok, text):
    """A late owner approval sends through no Controller, so the post-hook never sees that
    act (``approval_queue.run_approved_outbound``). Its row is written here, with its subject,
    so the repeat check knows the post went out. Fail-open."""
    try:
        from core.effects import classify_effect, record_external_write
        from core.verb_policy import VERB_POLICY
        row = VERB_POLICY.get(action_name)
        verdict = classify_effect(row.tool, action_name) if row is not None else None
        if verdict is None:
            return
        ref = _result_ref(types.SimpleNamespace(extracted_content=text), verdict.effect) if ok else ""
        record_external_write(
            effect=verdict.effect, tool=row.tool or "", action=action_name,
            target=_target(params), surface="owner_approval", autonomous=False,
            confidence=verdict.confidence, outcome="ok" if ok else "error",
            user_id=str(user_id or ""), fingerprint=_fingerprint(params),
            subject=str(params.get(row.subject) or "").strip() if row.subject else "",
            extra={"ref": ref} if ref else None)
    except Exception:
        logger.warning("approved send of %s: no row", action_name, exc_info=True)


def make_effect_record_hook(controller):
    """post_tool_call: record one ``external_write`` row per outward act.

    Register with ``fail_mode="open"`` — telemetry must never break execution.
    """
    async def _effect_record_hook(action_name, params, result, execution_context):
        try:
            tool_id, verdict = _classify(controller, action_name)
            if verdict is None or _lists_only(action_name, params):
                return
            _record(controller, action_name, params, execution_context,
                    tool_id, verdict, _outcome(result), result)
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
            from core.tool_gates import refusal_text
            reason = refusal_text("strict_code_change", action=action_name,
                                  effect=verdict.effect)
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


def make_repeat_act_hook(controller):
    """pre_tool_call: refuse a second good act on one declared subject.

    Register with ``fail_mode="open"``: the check reads telemetry, and a read fault must not
    stop the post, so it logs and allows. ``is_owner_driven`` is the wrong predicate here:
    it passes a standing job's main run, which is the run that repeats.
    """
    async def _repeat_act_hook(action_name, params, execution_context):
        if not _declared_subject(action_name):
            return None
        try:
            subject = _subject(controller, action_name, params)
            from core.security.owner_turn import is_owner_typing
            if not subject or is_owner_typing(execution_context, controller):
                return None
            from core.effects import last_good_act_on
            user_id = (getattr(execution_context, "user_id", None)
                       or getattr(controller, "user_id", "") or "")
            prior = await asyncio.to_thread(last_good_act_on, subject, user_id=str(user_id))
        except Exception:
            logger.warning("repeat check failed for %s (fail-open)", action_name, exc_info=True)
            return None
        if prior is None:
            logger.warning("repeat check: the event log is unreadable; %s passes", action_name)
            return None
        if not prior:
            return None
        from core.tool_gates import refusal_text
        ref = str(prior["attrs"].get("ref") or "")
        reason = refusal_text(
            "repeat_act", date=time.strftime("%Y-%m-%d", time.gmtime(prior["ts"])),
            ref=f" ({ref})" if ref else "")
        try:
            tool_id, verdict = _classify(controller, action_name)
            if verdict is not None:
                _record(controller, action_name, params, execution_context,
                        tool_id, verdict, "denied")
        except Exception:
            logger.debug("repeat denial record failed (fail-open)", exc_info=True)
        return reason

    return _repeat_act_hook


__all__ = ["make_effect_gate_hook", "make_effect_record_hook", "make_repeat_act_hook",
           "record_approved_send", "resolve_action_tool"]
