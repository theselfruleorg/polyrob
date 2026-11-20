"""The ONE way code (not the model) runs a money act on its own (proposal 122 W4).

The income rule's buy (``tools/income_rule/loop.py``) runs an exact act the OWNER set, with
no model turn. Nothing else runs here: a community's opinion (a poll, replies) is input the
agent weighs in its own turn, never a trigger for code to act. They add no
authority: the act runs in :func:`act_context` — the main-agent context of an autonomous
run, marked in ``core.autonomy_marker`` — so the money guard answers for it as for a goal or
a cron run (the trading flag and the unattended-money regime admit it or not; the pause, the
autonomous ceiling, the daily and per-transaction caps bind). The call goes through the seat
stages of the one policy chain (``policy_chain.run_seat_action``: the denylist, the effect
pause gate, the act record with the bound ``source``).

A live act the lane step would send to the owner (``spend_lane.spend_exemption`` is None)
never runs here: the result says ``needs_owner`` and the caller puts it on the owner's
decision (the income rule). Code never forces an owner act.
"""
import re
from types import SimpleNamespace
from typing import Any, Dict, Tuple

#: A transaction hash in a verb's report (the one pattern; the income rule reads it too).
TX_REF = re.compile(r"\b0x[0-9a-fA-F]{64}\b")


def act_context(user_id: str, session_id: str, phase: str) -> SimpleNamespace:
    """The context an autonomous act runs in: the main agent of an autonomous run (never the
    owner, never a seat). Marked autonomous so the guard's turn rules apply unchanged."""
    from core.autonomy_marker import mark_autonomous
    from core.money.authority import PAGE_REQUEST_KEY
    mark_autonomous(session_id)
    # PAGE_REQUEST_KEY: not the owner asking, whatever the marker says (owner_direct_turn).
    return SimpleNamespace(user_id=str(user_id or ""), session_id=session_id,
                           role="orchestrator", is_sub_agent=False,
                           metadata={"turn_id": f"{session_id}-{phase}",
                                     PAGE_REQUEST_KEY: True})


def _tool(tool_id: str, factory=None):
    if factory is not None:
        return factory(tool_id)
    from tools.descriptors import get_tool_class
    cls = get_tool_class(tool_id)
    if cls is None:
        raise RuntimeError(f"the {tool_id} tool is not available on this instance")
    return cls()


#: The end of one act: ``done`` (it happened), ``failed`` (it did not, nothing is owed back)
#: or ``unknown`` (it may have landed: never retried, never booked done — the owner settles it).
DONE, FAILED, UNKNOWN = "done", "failed", "unknown"
_META_OUTCOME = {"done": DONE, "confirmed": DONE, "success": DONE,
                 "failed": FAILED, "reverted": FAILED, "expired": FAILED, "not_sent": FAILED,
                 "unknown": UNKNOWN, "pending": UNKNOWN, "in_flight": UNKNOWN}
#: The verbs' one result line (``RESULT: CONFIRMED`` · ``REVERTED`` · ``EXPIRED`` · ``BROADCAST
#: BUT NOT CONFIRMED`` · ``UNCONFIRMED``).
_RESULT = re.compile(r"RESULT:\s*([A-Z][A-Z ]*)")
#: ``core.wallet.broadcast.evm.broadcast_failure_text`` of a send that may have landed.
_UNKNOWN_WORDS = ("outcome unknown", "may have landed", "do not send again",
                  "may still land")


def outcome(result: Any, *, live: bool) -> str:
    """The end of one act's result (:data:`DONE` · :data:`FAILED` · :data:`UNKNOWN`).

    An explicit ``metadata["outcome"]`` wins. Else an error is failed unless its words say
    the send may have landed; a live result is done only on ``RESULT: CONFIRMED``, failed on
    a revert or an expiry, and unknown otherwise — a live act whose end the result does not
    name is never booked done and never retried. A dry run is done only when the guard
    allowed it."""
    meta = getattr(result, "metadata", None) or {}
    named = _META_OUTCOME.get(str(meta.get("outcome") or "").strip().lower()) \
        if isinstance(meta, dict) else None
    if named:
        return named
    err = getattr(result, "error", None)
    text = str(getattr(result, "extracted_content", "") or "")
    if err:
        low = str(err).lower()
        return UNKNOWN if (live and any(w in low for w in _UNKNOWN_WORDS)) else FAILED
    if "NOT SENT" in text:
        return FAILED
    if not live:
        ok = (isinstance(meta, dict) and meta.get("allowed") is True) or "DRY RUN" in text
        return DONE if ok else FAILED
    m = _RESULT.search(text)
    word = m.group(1).strip() if m else ""
    if word.startswith("CONFIRMED"):
        return DONE
    if word.startswith(("REVERTED", "EXPIRED", "FAILED")):
        return FAILED
    return UNKNOWN


def verdict(result: Any, *, live: bool) -> Tuple[bool, str]:
    """``(ok, text)`` of one act's result: ok only when its :func:`outcome` is done."""
    err = getattr(result, "error", None)
    text = str(err) if err else str(getattr(result, "extracted_content", "") or "")
    return outcome(result, live=live) == DONE, text


async def run_act(*, action: str, tool_id: str, params: Dict[str, Any], why: str,
                  user_id: str, session_id: str, source: str, live: bool,
                  tool_factory=None) -> Dict[str, Any]:
    """Run one exact act through the full guard: a dry run (``live=False``) or the act.
    Returns ``{"ok", "outcome", "text", "tx_ref", "needs_owner"}``. Never raises."""
    from core.act_reason import (reset_act_source, reset_act_why, set_act_source,
                                 set_act_why)
    why_tok = set_act_why(why)
    src_tok = set_act_source(source)
    called = False
    try:
        params = dict(params or {}, dry_run=not live, why=why)
        if live:
            from core.config_policy.spend_lane import spend_exemption
            if not spend_exemption(action, params):
                return {"ok": False, "outcome": FAILED, "tx_ref": "", "needs_owner": True,
                        "text": (f"{action} with these params needs the owner's approval "
                                 f"(it is outside the autonomous lane). Nothing was sent.")}
        tool = _tool(tool_id, tool_factory)
        from core.action_names import bare_action_name
        fn = getattr(tool, bare_action_name(tool_id, action))
        model = getattr(fn, "_param_model", None)
        if model is not None and "why" not in model.model_fields:
            params.pop("why", None)
        validated = model(**params) if model is not None else params
        ctx = act_context(user_id, session_id, "run" if live else "check")
        from core.seat_runner import seat_runner
        runner = seat_runner()
        if runner is None:   # the policy chain is not loaded: never act around it
            return {"ok": False, "outcome": FAILED,
                    "text": "refused: the seat policy chain is not loaded",
                    "tx_ref": "", "needs_owner": False}
        run_seat_action, SeatRefused = runner
        called = True
        try:
            result = await run_seat_action(
                tool_id=tool_id, action=action, params=params, execution_context=ctx,
                call=lambda: fn(validated, execution_context=ctx))
        except SeatRefused as refused:
            return {"ok": False, "outcome": FAILED, "text": f"refused: {refused}", "tx_ref": "",
                    "needs_owner": False}
        end = outcome(result, live=live)
        ok, text = verdict(result, live=live)
        ref = TX_REF.search(text) if live else None   # a revert names its tx too
        return {"ok": ok, "outcome": end, "text": text[:2000],
                "tx_ref": ref.group(0) if ref else "", "needs_owner": False}
    except Exception as exc:
        # An exception once a live act was called may come after its broadcast: unknown.
        end = UNKNOWN if (live and called) else FAILED
        return {"ok": False, "outcome": end, "text": f"the act could not run: {exc}", "tx_ref": "",
                "needs_owner": False}
    finally:
        reset_act_source(src_tok)
        reset_act_why(why_tok)


__all__ = ["DONE", "FAILED", "UNKNOWN", "act_context", "outcome", "run_act", "verdict"]
