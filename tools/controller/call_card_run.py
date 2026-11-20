"""The call-card runner (proposal 126): raise a card that holds the exact call, and
run it once on the owner's tap.

* :func:`need_owner` — the ONE way a verb says "this needs the owner". The verb
  hands over the exact steps; this stores the card, pushes it and returns a typed
  result (``metadata["owner_act"]``). The model never writes the call text.
* :func:`on_press` — what every seat does with a call card's tap (Telegram, the
  console and the chat surfaces through ``surfaces/telegram/card_ops.py``, the
  REPL through ``cli/ui/commands/h_cards.py``). The seat only delivers the text.
* :func:`run_confirmed` — the steps, in order, as the OWNER SEAT
  (``owner_seat_context``) between the stages of the one policy chain
  (``run_seat_action``): the denylist, the effect gate, the guard, the wallet caps
  and every money gate run again. A card adds no authority: the owner seat could
  type the same call. Each finished step is recorded on the card, so a seat edits
  it in place; a stop after a finished step offers the rest as a new card.

NOTE: no ``from __future__ import annotations`` (the Controller mixin rule).
"""
import importlib
import logging
import typing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The tool behind each action-id prefix a call card may run. A test swaps the
#: factory for a fake tool.
TOOL_FACTORIES: Dict[str, Callable[[], Any]] = {}
_TOOL_CLASSES = {"defi_trade": "tools.defi.trade_tool:DefiTradeTool"}

#: How a Refresh rebuilds a card: ``kind`` → ``"module:function"``, called as
#: ``await fn(user_id, args, seat=…, container=…)`` and returning the reply text.
REBUILDERS = {"lp_declaration": "tools.defi.lp_move_card:rebuild"}

#: The owner writes a step may make (never a model tool call): ``name`` →
#: ``fn(user_id, args) -> (ok, text)``.
def _lp_seed_confirm(user_id: str, args: Dict[str, Any]) -> Tuple[bool, str]:
    from core.wallet import lp_seed
    ref = str(args.get("ref") or "")
    try:
        row = lp_seed.get(ref, user_id=str(user_id))
        if row["state"] == lp_seed.CONFIRMED:   # a Refresh after an earlier Confirm
            return True, f"declaration {ref} is already confirmed (one use, one hour)"
        row = lp_seed.confirm(ref, user_id=str(user_id))
    except lp_seed.SeedUnreadable as exc:
        return False, f"declaration {ref} NOT confirmed: {exc}. RESULT: NOT SENT."
    except lp_seed.SeedError as exc:
        return False, f"declaration {ref} NOT confirmed: {exc}. RESULT: NOT SENT."
    return True, (f"declaration {ref} confirmed (one use, one hour): "
                  + "; ".join(lp_seed.summary_lines(row["fields"])[:1]))


OWNER_WRITES: Dict[str, Callable[[str, Dict[str, Any]], Tuple[bool, str]]] = {
    "lp_seed_confirm": _lp_seed_confirm,
}

def seat_of(surface_id: str) -> str:
    """The seat a surface id runs as (``core.surfaces.seat_names.turn_seat``, the ONE map)."""
    from core.surfaces.seat_names import turn_seat
    return turn_seat(surface_id)


def _load(ref: str) -> Any:
    mod, _, attr = ref.partition(":")
    return getattr(importlib.import_module(mod), attr)


def _split(action: str) -> Tuple[str, str]:
    for prefix in sorted(set(_TOOL_CLASSES) | set(TOOL_FACTORIES), key=len, reverse=True):
        if action.startswith(prefix + "_"):
            return prefix, action[len(prefix) + 1:]
    raise ValueError(f"{action} is not an action a card can run")


def cardable(action: str) -> bool:
    """A step may run a registered MONEY action of a known tool, nothing else."""
    try:
        _split(action)
        from core.money.classify import money_action
        return bool(money_action(action))
    except Exception:  # noqa: BLE001
        return False


def check_steps(steps: List[Dict[str, Any]]) -> None:
    """ValueError unless every step is an owner write or a cardable money action."""
    from core.surfaces.call_cards import clean_steps
    for step in clean_steps(steps):
        if step["do"] not in OWNER_WRITES and not cardable(step["do"]):
            raise ValueError(f"{step['do']} cannot be run from a card")


def _tool(prefix: str) -> Any:
    factory = TOOL_FACTORIES.get(prefix)
    return factory() if factory else _load(_TOOL_CLASSES[prefix])()


async def _run_action(user_id: str, action: str, args: Dict[str, Any], *, seat: str,
                      session_id: Optional[str]) -> Tuple[bool, str]:
    """One money action as the owner seat. ``(ok, report)``; never raises."""
    from core.security.turn_facts import owner_seat_context
    from tools.defi.owner_verbs._seat import run_tool
    try:
        prefix, method = _split(action)
        tool = _tool(prefix)
        fn = getattr(tool, method)
        model = getattr(fn, "_param_model", None) or typing.get_type_hints(fn).get("params")
        params = model(**args)
    except Exception as exc:  # noqa: BLE001
        return False, f"{action} could not be built: {exc}. RESULT: NOT SENT."
    ctx = owner_seat_context(user_id, seat=seat, session_id=session_id)
    try:
        # The one owner-seat runner (a refusal comes back as the result's error).
        res = await run_tool(tool, tool_id=prefix, verb=method, params=params, ctx=ctx,
                             call=lambda: fn(params, ctx), as_owner=True)
    except Exception as exc:  # noqa: BLE001 — an unknown outcome, said so
        logger.error("call card %s raised", action, exc_info=True)
        return False, f"Command failed: {str(exc)[:200]} — outcome unknown."
    text = str(getattr(res, "error", None) or getattr(res, "extracted_content", None)
               or "No result was reported.")
    from core.surfaces.cards import failed_result
    return (not getattr(res, "error", None) and not failed_result(text)), text


def _origin(ctx: Any) -> str:
    from core.surfaces import cards
    meta = getattr(ctx, "metadata", None) or {}
    return cards.ORIGIN_SYSTEM if isinstance(meta, dict) and meta.get("owner_seat") \
        else cards.ORIGIN_AGENT


def _refusal(ctx: Any, user_id: str) -> Optional[str]:
    """Who may put a call card to the owner. A room, a correspondent's session and a
    forged turn never reach a money verb (the policy chain refuses them first)."""
    try:
        from tools.controller.owner_ask_action import _is_leaf
        if _is_leaf(ctx):
            return "a leaf/sub-agent reports to its parent, never to the owner"
        # An autonomous run (goal, cron, planner, an income-rule act) gets no card: a money
        # call is never one tap away from an unattended run, and its own durable ask (or the
        # act's needs-owner result) keeps the run's state true. The test approve_verb's
        # grant card uses; a probe fault refuses.
        from core.security.owner_turn import is_autonomous_session
        if is_autonomous_session(ctx, None):
            return "an autonomous run (goal, cron, planner) never puts a money call to a card"
        from core.instance import is_owner_uid
        if not is_owner_uid(user_id):
            return "cards go to the owner, and this tenant is not the owner"
    except Exception:  # noqa: BLE001
        return "the owner could not be resolved"
    return None


async def need_owner(tool: Any, ctx: Any, *, title: str, body: str,
                     steps: List[Dict[str, Any]], rebuild: Optional[Dict[str, Any]] = None,
                     lead: str = "", user_id: Optional[str] = None) -> Any:
    """The typed "owner act needed" result: the card holding ``steps`` is stored and
    pushed, and the result names it. ``lead`` opens the result text; ``user_id`` is the
    caller the verb already resolved (else the context's)."""
    from core.surfaces import call_cards, cards
    user_id = str(user_id or getattr(ctx, "user_id", "") or "")
    why = _refusal(ctx, user_id)
    if why:
        return tool._ar(error=f"refused: this needs the owner, and {why}. No card was made. "
                              "RESULT: NOT SENT.")
    try:
        check_steps(steps)
        same = _open_same(user_id, steps)
        if same is not None:
            return _owner_act_result(same, steps, "already", lead=lead)
        sid = str(getattr(ctx, "session_id", "") or "") or None
        card = call_cards.make(user_id, steps, title=title, body=body, rebuild=rebuild,
                               origin=_origin(ctx), session_id=sid)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(error=f"refused: no card could be made ({exc}). RESULT: NOT SENT.")
    outcome = "failed"
    try:
        container = getattr(tool, "container", None)
    except Exception:  # noqa: BLE001 — a tool whose container is not built yet
        container = None
    try:
        outcome = await cards.push(container, user_id, card, session_id=card.session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("call card %s: delivery failed: %s", card.card_id, exc)
    return _owner_act_result(card, steps, outcome, lead=lead)


def _open_same(user_id: str, steps: List[Dict[str, Any]]) -> Any:
    """The open card that already holds this call (a retry), or None."""
    from core.surfaces import call_cards
    try:
        return call_cards.open_same(user_id, steps)
    except Exception:  # noqa: BLE001 — an unreadable store: a new card, said in the log
        logger.warning("call card: open cards unreadable; a new card is made", exc_info=True)
        return None


def _owner_act_result(card: Any, steps: List[Dict[str, Any]], outcome: str, *,
                      lead: str = "") -> Any:
    """The typed "owner act needed" result naming ``card``. ``outcome`` is the push's
    answer, or ``already`` for an open card that holds the same call."""
    where = ("already open for the owner — this call made no second card"
             if outcome == "already"
             else "on the owner's chat" if outcome in ("sent", "queued")
             else "in the owner's /cards list (no live chat reached)")
    text = ((lead + "\n" if lead else "")
            + f"NOT sent yet: card {card.card_id} ({card.title}) is {where}. ONE tap on "
              f"Confirm runs the exact call it shows. Do not call it again, do not give the "
              f"owner a command to type and do not say it is done: cards_read shows its "
              f"outcome.")
    from agents.task.loop_types.controller_types import ActionResult
    return ActionResult(extracted_content=text, metadata={"owner_act": {
        "card_id": card.card_id, "steps": [s["do"] for s in steps], "pushed": outcome}})


#: The room a card's bound leaves above the value its dry run showed (owner, 2026-11-21).
MARGIN = 0.05


def bound_above(value_usd: Any, declared: Any) -> Any:
    """The card's ``max_spend_usd``: the shown value + :data:`MARGIN`, never above
    what the call declared; the declared bound when there is no shown value."""
    try:
        v = float(value_usd)
        if v > 0:
            b = round(v * (1 + MARGIN) + 0.01, 2)
            return min(b, float(declared)) if isinstance(declared, (int, float)) else b
    except (TypeError, ValueError):
        pass
    return declared


def call_body(action: str, args: Dict[str, Any], *, why: str = "") -> str:
    """What the owner reads on a card of one call: every money field whole."""
    try:
        from tools.controller.grant_card import card_context_lines, render_grant_card
        text = render_grant_card(action, args, "", reply_lines=False,
                                 context_lines=card_context_lines(action, args))
    except Exception:  # noqa: BLE001
        text = "\n".join(f"{k}: {v}" for k, v in args.items())
    return (why.strip() + "\n\n" if why.strip() else "") + text


async def owner_lane_card(tool: Any, ctx: Any, *, value_usd: Any = None, why: str = "",
                          title: str = "") -> Any:
    """126 wave 3: the action running now was refused ONLY for want of the owner (the
    guard's owner lane: above the autonomous ceiling, PAYOUT, an owner-only shape, a
    launch). Its EXACT call becomes ONE call card at the shown bound; Confirm runs it as
    the owner seat, where those lanes pass and every other gate runs again. None when
    there is no such call (a dry run raises nothing; an action no card can run)."""
    from tools.base_tool import current_call
    call = current_call()
    if not call:
        return None
    action, params = call
    if not cardable(action):
        return None
    try:
        args = {k: v for k, v in params.model_dump().items() if v is not None}
    except Exception:  # noqa: BLE001
        return None
    if args.get("dry_run") is True:
        return None
    if "dry_run" in args:
        args["dry_run"] = False
    if "max_spend_usd" in args:
        args["max_spend_usd"] = bound_above(value_usd, args["max_spend_usd"])
    try:
        from core.surfaces.call_cards import clean_steps
        clean_steps([{"do": action, "args": args}])
    except ValueError:
        return None     # a param a card cannot hold (a list, a nested map)
    res = await need_owner(tool, ctx, title=title or f"Approve: {action}",
                           body=call_body(action, args, why=why),
                           steps=[{"do": action, "args": args}])
    return None if getattr(res, "error", None) else res


class _Seat:
    """The tool-shaped holder ``need_owner`` needs when no tool runs the call (the
    approval queue parks a call before any tool sees it)."""

    def __init__(self, container: Any = None):
        self.container = container

    @staticmethod
    def _ar(*, content: Optional[str] = None, error: Optional[str] = None, metadata=None):
        from agents.task.loop_types.controller_types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content, metadata=metadata)


async def card_for_parked_call(action: str, params: Dict[str, Any], ctx: Any,
                               container: Any = None) -> Optional[str]:
    """126 wave 5 (owner, 2026-11-21): a MONEY call the approval queue would park
    becomes ONE call card holding that exact call; Confirm runs it once as the owner
    seat (no wait, no retry, no grant hash to match). Returns the agent's text, or
    None when a card cannot carry it (a non-money tool keeps the queue)."""
    if not cardable(action):
        return None
    args = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        from core.surfaces.call_cards import clean_steps
        clean_steps([{"do": action, "args": args}])
    except ValueError:
        return None
    res = await need_owner(_Seat(container), ctx, title=f"Approve: {action}",
                           body=call_body(action, args), steps=[{"do": action, "args": args}])
    return None if getattr(res, "error", None) else str(res.extracted_content)


def _report(lines: List[str]) -> str:
    return "\n".join(lines).strip()


async def run_confirmed(card: Any, *, seat: str, container: Any = None) -> str:
    """Run a CONFIRMED call card's steps once and finish the card. Returns its report."""
    from core.surfaces import call_cards, cards
    try:
        steps = call_cards.steps_of(card)
    except ValueError as exc:
        text = f"refused: {exc}. RESULT: NOT SENT."
        cards.finish(card.card_id, text)
        return text
    start = call_cards.done_count(card)
    lines: List[str] = [f"{card.title}:"]
    ok, i = True, start
    for i in range(start, len(steps)):
        step = steps[i]
        label = f"step {i + 1}/{len(steps)} ({step['do']})"
        if step["do"] in OWNER_WRITES:
            try:
                ok, text = OWNER_WRITES[step["do"]](card.user_id, step["args"])
            except Exception as exc:  # noqa: BLE001
                ok, text = False, f"{exc}. RESULT: NOT SENT."
        else:
            ok, text = await _run_action(card.user_id, step["do"], step["args"], seat=seat,
                                         session_id=card.session_id)
        lines.append(f"{label}: {'done' if ok else 'STOPPED'}\n{text}")
        if not ok:
            break
        call_cards.progress(card.card_id, i + 1, _report(lines))
    report = _report(lines)
    if ok:
        cards.finish(card.card_id, report)
        return report
    finished = i > start
    from core.surfaces.card_states import OUTCOME_UNKNOWN, outcome_of
    if finished and outcome_of(report, True) != OUTCOME_UNKNOWN:
        report += await _continuation(card, steps[i:], container)
    elif not finished:
        report += "\nNothing ran after this step."
    cards.finish(card.card_id, report)
    return report


async def _continuation(card: Any, rest: List[Dict[str, Any]], container: Any) -> str:
    """A stop after a finished step: the rest of the flow is its own card."""
    from core.surfaces import call_cards, cards
    try:
        with cards.lineage(card):
            nxt = call_cards.make(card.user_id, rest, title=f"Continue: {card.title}",
                                  body=f"The earlier steps of card {card.card_id} finished; "
                                       f"this runs the rest:\n"
                                       + "\n".join(f"- {s['do']}" for s in rest),
                                  origin=cards.ORIGIN_SYSTEM)
        await cards.push(container, card.user_id, nxt, session_id=nxt.session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("call card %s: continuation not made: %s", card.card_id, exc)
        return "\nThe rest could not be offered as a card; read the report above."
    return f"\nThe rest of the flow is card {nxt.card_id} (one tap)."


async def refresh(card: Any, *, seat: str, container: Any = None) -> str:
    """A fresh dry run and a new card for a REPLACED call card."""
    from core.surfaces import call_cards, cards
    spec = call_cards.rebuild_of(card)
    if not spec or spec.get("kind") not in REBUILDERS:
        text = "This card cannot refresh itself. Ask the agent for the action again."
    else:
        try:
            with cards.lineage(card):
                text = await _load(REBUILDERS[spec["kind"]])(
                    card.user_id, dict(spec.get("args") or {}), seat=seat, container=container)
        except Exception as exc:  # noqa: BLE001
            logger.warning("call card %s: refresh failed", card.card_id, exc_info=True)
            text = f"No fresh card: {str(exc)[:200]}"
    cards.note_result(card.card_id, text)
    return text


async def on_press(press: Any, *, seat: str, container: Any = None,
                   deliver: Optional[Callable[[str], Awaitable[Any]]] = None) -> str:
    """A seat's whole handling of a call card's tap. With ``deliver`` a Confirm runs in
    the background (a move waits for two receipts) and the report is delivered."""
    card = press.card
    if press.call == "re":
        return await refresh(card, seat=seat, container=container)
    if deliver is None:
        return await run_confirmed(card, seat=seat, container=container)

    async def _bg() -> None:
        text = await run_confirmed(card, seat=seat, container=container)
        try:
            from core.surfaces.money_report import owner_view
            await deliver(owner_view(text))
        except Exception:  # noqa: BLE001
            logger.error("call card %s: report not delivered", card.card_id, exc_info=True)

    from core.async_bridge import spawn_retained
    spawn_retained(_bg(), _RUNNING)
    return f"⏳ Started: {card.title} (card {card.card_id}). The result comes here."


_RUNNING: set = set()

__all__ = ["MARGIN", "OWNER_WRITES", "card_for_parked_call", "REBUILDERS", "TOOL_FACTORIES", "bound_above", "call_body",
           "cardable", "check_steps", "need_owner", "on_press", "owner_lane_card", "refresh",
           "run_confirmed", "seat_of"]
