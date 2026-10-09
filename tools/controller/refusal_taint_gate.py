"""The refusal taint gate: a run whose money verb was refused posts nothing public.

Prod 2026-09-26 04:02 (the PNL buyback): ``defi_trade_swap`` was refused by the
identity gate, and the same cron run then called ``message`` to the channel
``@example_channel``, ``message`` to the group ``-1002125904710`` and
``twitter_post`` — each carrying the refusal internals. The skill said
"refusals go to the owner only"; nothing in code did.

Two Controller hooks, registered in ``Controller.__init__`` next to the
financial-claim gate (never post-hoc: a delegated sub-agent builds a fresh
Controller):

- **record** (post, fail-open): a MONEY-effect verb (the ONE classification,
  ``core.effects.classify_effect``) that returned an error taints the run
  (``core.security.refusal_taint``). A money verb another pre-hook VETOED is
  marked by :func:`note_denied_action`, called from the veto branch of
  ``multi_act`` — a vetoed action never reaches the post hooks. An error that
  only names an unmet precondition or a market condition (insufficient
  allowance / balance, no route, stale quote, revert, RPC) does not taint
  (``core.security.refusal_taint.taints``); a veto always taints.
- **gate** (pre, fail-closed): in a tainted run, an outbound verb whose
  recipient is not the owner is refused with
  :data:`core.security.refusal_taint.PUBLIC_REFUSAL_TEXT`.

Outbound = the send-time text table (``financial_claim_gate.OUTBOUND_TEXT_FIELDS``)
plus any verb the effect table calls ``social`` or ``comms``, plus the
text-bearing ``public`` verbs in :data:`PUBLIC_TEXT_VERBS`. The owner lane stays
open: ``message`` to the owner (any spelling, or the omitted/``owner`` default),
``email_send`` to the owner's address, ``send_message`` outside a room, and every
verb that is not outbound at all (``owner_ask``, ``done``, reads, trades).

NOTE: no ``from __future__ import annotations`` in this package.
"""
import logging

logger = logging.getLogger(__name__)

#: ``public``-effect verbs that carry words to the world (not code deploys).
PUBLIC_TEXT_VERBS = frozenset({"publish", "github_issue_create", "github_pr_comment"})

#: Effect classes whose every verb reaches a recipient other than this box.
_OUTBOUND_EFFECTS = frozenset({"social", "comms"})


def _session_id(execution_context, controller):
    return str(getattr(execution_context, "session_id", "")
               or getattr(controller, "session_id", "") or "")


def _user_id(execution_context, controller):
    return str(getattr(execution_context, "user_id", "")
               or getattr(controller, "user_id", "") or "")


def _effect(controller, action_name):
    try:
        from tools.controller.effect_hooks import _classify
        _tool_id, verdict = _classify(controller, action_name)
        return getattr(verdict, "effect", None)
    except Exception:
        return None


def _params_dict(params):
    if isinstance(params, dict):
        return params
    try:
        return params.model_dump()
    except Exception:
        return {}


def _owner_targets(controller, user_id):
    """Per-surface owner addresses (the one resolver ``message`` uses)."""
    from tools.controller.message_send import build_owner_targets
    return build_owner_targets(getattr(controller, "container", None), user_id)


def _is_owner(controller, user_id, surface, target):
    from core.surfaces.outbound_target import is_owner_target
    from tools.controller.message_send import resolve_message_defaults
    owner_targets = _owner_targets(controller, user_id) or {}
    sfc, tgt = resolve_message_defaults(surface, target, owner_targets)
    if str(tgt).strip().lower() == "owner":
        return True
    return is_owner_target(sfc, tgt, owner_targets)


def _bound_to_room(controller):
    try:
        from core.surfaces.room_keys import is_group_session_key
        orch = getattr(controller, "orchestrator", None)
        return is_group_session_key(getattr(orch, "_chat_session_key", None))
    except Exception:
        return True  # cannot prove the bound chat is private: treat it as public


def is_outbound(controller, action_name):
    from tools.controller.financial_claim_gate import OUTBOUND_TEXT_FIELDS
    name = str(action_name or "")
    if name in OUTBOUND_TEXT_FIELDS or name in PUBLIC_TEXT_VERBS:
        return True
    return _effect(controller, name) in _OUTBOUND_EFFECTS


def reaches_non_owner(controller, action_name, params, user_id):
    """True when this outbound call's recipient is anyone but the owner."""
    p = _params_dict(params)
    if action_name == "send_message":
        return _bound_to_room(controller)
    if action_name == "message":
        return not _is_owner(controller, user_id, p.get("surface"), p.get("target"))
    if action_name == "email_send":
        # EVERY recipient (to, cc, bcc) must be the owner; a reply/forward
        # (recipients read from a received mail) falls through to True.
        addrs = []
        for key in ("to", "cc", "bcc"):
            value = p.get(key) or []
            items = value if isinstance(value, list) else str(value).replace(";", ",").split(",")
            addrs += [str(a).strip() for a in items if str(a).strip()]
        return not (addrs and all(_is_owner(controller, user_id, "email", a) for a in addrs))
    return True


def note_denied_action(controller, action_name, execution_context, reason=""):
    """A pre-hook vetoed ``action_name``. A vetoed MONEY verb taints the run.
    Fail-open: bookkeeping never changes the veto."""
    try:
        if _effect(controller, action_name) != "money":
            return
        from core.security.refusal_taint import mark
        mark(_session_id(execution_context, controller), action=str(action_name),
             user_id=_user_id(execution_context, controller), detail=str(reason or ""))
    except Exception:
        logger.debug("refusal taint: veto note skipped", exc_info=True)


def make_refusal_taint_record_hook(controller):
    """post_tool_call (register ``fail_mode="open"``): a money verb that returned
    an error taints its run."""
    async def _refusal_taint_record(action_name, params, result, execution_context):
        try:
            err = getattr(result, "error", None)
            if not err or _effect(controller, action_name) != "money":
                return
            from core.security.refusal_taint import error_kind, mark, taints
            if not taints(str(err), error_kind(result)):
                # An unmet precondition / market error (insufficient allowance,
                # no route, stale quote, RPC): not a refusal of an unsafe act.
                logger.info("refusal taint: %s precondition error does not taint",
                            action_name)
                return
            mark(_session_id(execution_context, controller), action=str(action_name),
                 user_id=_user_id(execution_context, controller), detail=str(err))
        except Exception:
            logger.debug("refusal taint: record skipped", exc_info=True)

    return _refusal_taint_record


def make_refusal_taint_gate_hook(controller):
    """pre_tool_call (register ``fail_mode="closed"``): in a refusal-tainted run,
    refuse an outbound verb that reaches anyone but the owner."""
    async def _refusal_taint_gate(action_name, params, execution_context):
        from core.security.refusal_taint import PUBLIC_REFUSAL_TEXT, is_tainted
        sid = _session_id(execution_context, controller)
        if not is_tainted(sid) or not is_outbound(controller, action_name):
            return None
        uid = _user_id(execution_context, controller)
        if not reaches_non_owner(controller, str(action_name), params, uid):
            return None
        logger.warning("⛔ %s withheld: run %s had a refused money action",
                       action_name, sid[:8])
        try:
            from core.security.refusals import record_refusal
            record_refusal("refusal_withheld", tool=str(action_name), user_id=uid,
                           session_id=sid, detail="public send after a money refusal")
        except Exception:
            logger.debug("refusal taint: refusal record skipped", exc_info=True)
        return PUBLIC_REFUSAL_TEXT

    return _refusal_taint_gate


__all__ = ["PUBLIC_TEXT_VERBS", "is_outbound", "make_refusal_taint_gate_hook",
           "make_refusal_taint_record_hook", "note_denied_action", "reaches_non_owner"]
