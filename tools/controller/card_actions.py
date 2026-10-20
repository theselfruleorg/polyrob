"""Action cards from the agent: ``present_choice`` and ``propose_action``.

Before these (crypto-UX review, 2026-09-27) the agent could put a question to
the owner only as prose, and a money action only as a sentence the owner had to
turn into a typed command. Both are now cards (``core/surfaces/cards.py``):

* ``present_choice(question, options)`` — 2 to 6 options as buttons on
  Telegram, numbered in the terminal. The tool WAITS for the pick and returns
  it into this turn. The pick is stored text the owner tapped, never a model
  paraphrase.
* ``propose_action(command, why)`` — a money verb and its arguments (e.g.
  ``/send 0.5 native to 0x… on base``). The card has NO confirm: its one
  action runs the verb's QUOTE on the owner's seat, which answers with a
  system quote card whose Confirm the owner taps. The model never writes a
  confirm button, a price or a bound, and nothing moves from its card.

Controller-level (the ``owner_ask_action`` pattern — ``action_registration.py``
is at its size ceiling), so every session can reach them.

⚠️ Refused in a room, for a leaf/sub-agent, in a session a correspondent
drives, and for a tenant that is not the owner — the same gates as
``owner_ask``. ``present_choice`` is also refused in an autonomous run (the run
cannot wait for a person; ``owner_ask`` is the durable question for that).
"""
import asyncio
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

#: How long ``present_choice`` waits by default, and at most.
DEFAULT_WAIT_S = 300
MAX_WAIT_S = 900
#: Headroom over the wait for delivery, the expiry transition and the result.
_TIMEOUT_GRACE_S = 30


def choice_action_timeout(params, base: float) -> float:
    """The controller's timeout for one ``present_choice`` call: never less
    than the wait it declared (the generic 180 s cut a 300 s wait, 2026-10-15)."""
    try:
        wait = int((params or {}).get("wait_seconds") or DEFAULT_WAIT_S)
    except (TypeError, ValueError, AttributeError):
        wait = DEFAULT_WAIT_S
    return max(float(base), float(min(wait, MAX_WAIT_S) + _TIMEOUT_GRACE_S))


def _refusal(controller, execution_context, user_id: str) -> Optional[str]:
    from core.surfaces.room_policy import is_public_session
    orch = getattr(controller, "orchestrator", None)
    if is_public_session(orch):
        return "Refused: a room session cannot put a card to the owner."
    from tools.controller.owner_ask_action import _is_leaf
    if _is_leaf(execution_context):
        return ("Refused: a leaf/sub-agent reports to its parent, never to the owner.")
    from agents.task.session_class import correspondent_facing
    if correspondent_facing(orch):
        return ("Refused: a session holding a correspondent's message cannot put a "
                "card to the owner.")
    try:
        from core.config_policy import local_mode_enabled
        from core.instance import is_owner_local_safe, resolve_owner_principal
        if not is_owner_local_safe(user_id, owner_principal=resolve_owner_principal(),
                                   local_enabled=local_mode_enabled()):
            return "Refused: cards go to the owner, and this tenant is not the owner."
    except Exception:
        return "Refused: the owner could not be resolved."
    return None


def _session_id(controller, execution_context) -> str:
    return str(getattr(execution_context, "session_id", "")
               or getattr(controller, "session_id", "") or "")


def _joined(group_id: str, then: str) -> None:
    try:
        from core.surfaces import card_groups as G
        from core.surfaces import cards
        G.rejoin(group_id, cards.store())
        if then:
            G.groups(cards.store()).set_then(group_id, then)
    except Exception:
        logger.warning("card group %s: the follow-up was not stored", group_id,
                       exc_info=True)


def _group_for(user_id: str, session_id: str) -> Optional[str]:
    """084: the session's open card group (a goal or cron run names its goal /
    job as the origin). None without a session — such a card wakes nobody."""
    if not session_id:
        return None
    try:
        from core.autonomy_marker import (cron_job_for_session, goal_for_session,
                                          is_autonomous)
        from core.surfaces import card_groups as G
        origin, ref = G.ORIGIN_CHAT, ""
        if is_autonomous(session_id):
            goal, job = goal_for_session(session_id), cron_job_for_session(session_id)
            origin, ref = ((G.ORIGIN_GOAL, goal) if goal else
                           (G.ORIGIN_CRON, job) if job else (G.ORIGIN_CRON, ""))
        from core.surfaces import cards
        gs = G.groups(cards.store())
        return gs.open_group_for(user_id, session_id, origin=origin, origin_ref=ref or "")
    except Exception:
        logger.warning("card group could not be opened — the card wakes nobody",
                       exc_info=True)
        return None


async def _deliver(controller, user_id: str, card, session_id: str) -> str:
    from core.surfaces import cards
    from core.surfaces.user_delivery import deliver_user_message
    try:
        return await deliver_user_message(
            getattr(controller, "container", None), user_id, cards.render_text(card),
            source="action_card", session_id=session_id or None, card_id=card.card_id)
    except Exception as e:
        logger.warning("action card delivery failed: %s", e)
        return "failed"


async def _terminal_pick(card) -> Optional[str]:
    """In the REPL: ask for the number right here (an explicit prompt answer,
    like the approval y/n — not a word parsed out of chat). None = no terminal."""
    from core.approval_input import get_approval_input
    reader = get_approval_input()
    if reader is None:
        return None
    from core.surfaces import cards
    n = len(card.options)
    prompt = cards.render_text(card) + f"\nType 1-{n} (anything else = no answer): "
    try:
        answer = (await reader(prompt) or "").strip()
    except Exception:
        return None
    return answer if answer.isdigit() and 1 <= int(answer) <= n else ""


def _send_recipient(args) -> Optional[str]:
    """The address after ``to`` in /send arguments, or None."""
    low = [str(a).lower() for a in args]
    if "to" in low and low.index("to") + 1 < len(args):
        return str(args[low.index("to") + 1])
    return None


def _open_sends_to(cards, user_id: str, words) -> list:
    """OPEN /send proposal cards that pay the same address as ``words``."""
    if not words or words[0].lower() != "/send":
        return []
    to = _send_recipient(words[1:])
    if not to:
        return []
    from core.wallet.addresses import same_address
    try:
        open_ = cards.store().open_cards(user_id, limit=100)
    except Exception:
        return []
    return [c for c in open_ if c.kind == cards.KIND_PROPOSAL and c.verb == "/send"
            and _send_recipient(c.args) and same_address(_send_recipient(c.args), to)]


def _send_precheck(words) -> Optional[str]:
    """Why a /send card would be dead on arrival, or None — the seat's OWN parser
    (``tools.defi.send_order``) and the address-poisoning rule, before the card
    exists (2026-10-17: six cards were proposed twice and none could quote). The
    full dry run stays on the owner's tap: an agent tool never builds an owner
    context (tests/test_owner_context_ratchet.py)."""
    if not words or words[0].lower() != "/send":
        return None
    from tools.defi.send_order import parse
    order, err = parse(words[1:])
    if err:
        return err.split("\n\n")[0]
    try:
        from core.wallet import address_lookalike
        poisoned = address_lookalike.poisoning_refusal(
            order["to"], chain="solana" if order["chain"] == "solana" else "evm")
    except Exception:
        poisoned = None
    return poisoned


def register_card_actions(controller) -> None:
    class PresentChoice(BaseModel):
        question: str = Field(..., min_length=4, max_length=200,
                              description="The one question the owner answers with a tap.")
        options: List[str] = Field(..., min_length=2, max_length=6,
                                   description="2 to 6 short options (each ≤ 80 characters).")
        wait_seconds: int = Field(DEFAULT_WAIT_S, ge=10, le=MAX_WAIT_S,
                                  description="How long to wait for the tap.")

    @controller.registry.action(
        "Ask the OWNER to pick one of 2-6 options, as buttons (Telegram) or a numbered "
        "prompt (terminal), and WAIT for the pick in this turn. Use it when you need the "
        "owner's choice to continue now. Not in autonomous runs (use owner_ask there).",
        param_model=PresentChoice,
    )
    async def present_choice(params: PresentChoice, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        sid = _session_id(controller, execution_context)
        try:
            from core.autonomy_marker import is_autonomous
            if sid and is_autonomous(sid):
                return ActionResult(
                    error=("Refused: an autonomous run cannot wait for a tap. Raise "
                           "owner_ask(question=\"… A) … or B) …\") — the answer reaches "
                           "this rail's next run."), include_in_memory=True)
        except Exception:
            pass
        from core.surfaces import cards
        try:
            card = cards.choice_card(user_id, params.question, list(params.options),
                                     session_id=sid or None,
                                     ttl=params.wait_seconds + 60)
        except ValueError as e:
            return ActionResult(error=f"present_choice: {e}", include_in_memory=True)
        typed = await _terminal_pick(card)
        if typed is not None:
            if typed:
                p = cards.press(card.card_id, typed, user_id)
                if p.card is not None and p.card.answer:
                    return ActionResult(
                        extracted_content=f"The owner picked: {p.card.answer}",
                        include_in_memory=True)
            cards.store().transition(card.card_id, frozenset({cards.S_OPEN}),
                                     cards.S_CANCELLED)
            return ActionResult(extracted_content="The owner gave no answer. Do not assume one.",
                                include_in_memory=True)
        outcome = await _deliver(controller, user_id, card, sid)
        if outcome not in ("sent", "queued"):
            cards.store().transition(card.card_id, frozenset({cards.S_OPEN}),
                                     cards.S_CANCELLED)
            return ActionResult(
                error=(f"The card could not reach the owner live ({outcome}); no answer "
                       f"will come. Ask in your reply instead, or raise owner_ask."),
                include_in_memory=True)
        got = await asyncio.to_thread(cards.wait_for_answer, card.card_id,
                                      float(params.wait_seconds))
        if got is not None and got.state == cards.S_DONE and got.answer:
            return ActionResult(extracted_content=f"The owner picked: {got.answer}",
                                include_in_memory=True)
        if got is not None and (got.result or "").startswith(cards.TEXT_REPLY_RESULT):
            return ActionResult(
                extracted_content=("The owner did not tap an option; they replied in text "
                                   "instead (queued for you as their next message). Read "
                                   "that message as their answer; do not assume an option."),
                include_in_memory=True)
        if got is not None and got.state == cards.S_OPEN:
            expired = cards.store().transition(card.card_id, frozenset({cards.S_OPEN}),
                                               cards.S_EXPIRED)
            if expired is not None:
                cards._notify(expired)
            else:   # picked in the last instant
                late = cards.store().get(card.card_id)
                if late is not None and late.answer:
                    return ActionResult(extracted_content=f"The owner picked: {late.answer}",
                                        include_in_memory=True)
        return ActionResult(
            extracted_content=(f"No pick within {params.wait_seconds}s (the card is closed). "
                               f"Do not assume an answer; continue with the safe reading or "
                               f"ask again later."),
            include_in_memory=True)

    class ProposeAction(BaseModel):
        command: str = Field(..., min_length=3, max_length=600, description=(
            "The owner money verb and its arguments, WITHOUT `go`, exactly as the owner "
            "would type it, e.g. `/send 0.5 native to 0xAbc… on base`, `/swap 0.1 native "
            "to 0xToken… on base`, `/bridge solana base "
            "0.9`, `/pay https://api.x/y 0.10`, `/claim 0xToken…`."))
        why: str = Field("", max_length=600, description="One line: why you propose it.")
        replaces: List[str] = Field(default_factory=list, max_length=20, description=(
            "Ids of OPEN cards this one supersedes (they are closed as replaced) — "
            "e.g. a re-split payout to the same address."))
        also_owed: bool = Field(False, description=(
            "true only when an open /send card already pays this address AND both "
            "payments are owed (e.g. two different bounty rounds)."))
        then: str = Field("", max_length=400, description=(
            "What you will do once the owner has decided every card you raised in this "
            "conversation. You are woken with each card's outcome; this text is shown "
            "back to you then."))

    @controller.registry.action(
        "Propose a money action to the OWNER as a card. Nothing moves: the owner taps "
        "'Get a quote', sees the real simulated price and caps, and only then can "
        "Confirm. Use it instead of describing a command for the owner to type, and when "
        "the owner only ASKS about a money move (execute one only on an imperative line). Verbs: "
        "/send /swap /bridge /pay /claim /launch /deploy /nft /identity "
        "/writeoff /unquarantine /wallet (trust|untrust), and /restart or /update (your own background "
        "service: it finishes its work, restarts and reports back).",
        param_model=ProposeAction,
    )
    async def propose_action(params: ProposeAction, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        words = (params.command or "").split()
        dead = _send_precheck(words)
        if dead:
            return ActionResult(error=(f"propose_action: this /send could not quote, so "
                                       f"no card was made: {dead}"), include_in_memory=True)
        from core.surfaces import cards
        clash = _open_sends_to(cards, user_id, words)
        unlisted = [c for c in clash if c.card_id not in params.replaces]
        if unlisted and not params.also_owed:
            listed = "; ".join(f"{c.card_id} ({c.line})" for c in unlisted)
            return ActionResult(error=(
                f"An open card already pays this address: {listed}. Pass "
                f"replaces=[<id>] to supersede it, or also_owed=true if BOTH payments "
                f"are owed. Nothing was proposed."), include_in_memory=True)
        sid = _session_id(controller, execution_context)
        group_id = _group_for(user_id, sid)
        try:
            card = cards.proposal_card(user_id, words[0] if words else "", words[1:],
                                       why=params.why, session_id=sid or None,
                                       group_id=group_id)
        except ValueError as e:
            return ActionResult(error=f"propose_action: {e}", include_in_memory=True)
        if group_id:
            # Only once the card exists: a refused proposal leaves no follow-up.
            _joined(group_id, (params.then or "").strip())
        replaced = []
        for cid in params.replaces:
            # Only this owner's own PROPOSAL cards: an id is not authority to close
            # an owner's quote awaiting Confirm, a choice, or another tenant's card.
            old = cards.store().get(cid)
            if old is None or old.user_id != user_id or old.kind != cards.KIND_PROPOSAL:
                continue
            done = cards.store().transition(cid, frozenset({cards.S_OPEN}), cards.S_REPLACED,
                                            result=f"replaced by card {card.card_id}")
            if done is not None:
                replaced.append(cid)
                cards._notify(done)
        outcome = await _deliver(controller, user_id, card,
                                 _session_id(controller, execution_context))
        where = ("on the owner's chat" if outcome == "sent"
                 else "queued for the owner's chat (quiet hours)" if outcome == "queued"
                 else "in the owner's /cards list (no live chat reached)")
        return ActionResult(
            extracted_content=(f"Proposed card {card.card_id} is {where}: {card.line}. "
                               f"NOTHING was sent or spent. The owner taps 'Get a quote', then "
                               f"Confirm. Do not say it is done, queued or pending approval."
                               + (" When the owner has decided every card you raised, you are "
                                  "woken with each card's outcome — do not ask the owner to "
                                  "tell you." if group_id else "")
                               + (f" Replaced (closed): {', '.join(replaced)}." if replaced else "")),
            include_in_memory=True)
