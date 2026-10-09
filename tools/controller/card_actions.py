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
            from agents.task.goals.autonomy_marker import is_autonomous
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

    @controller.registry.action(
        "Propose a money action to the OWNER as a card. Nothing moves: the owner taps "
        "'Get a quote', sees the real simulated price and caps, and only then can "
        "Confirm. Use it instead of describing a command for the owner to type. Verbs: "
        "/send /swap /bridge /pay /claim /launch /deploy /nft /identity "
        "/writeoff /unquarantine /wallet (trust|untrust), and /adopt <id> (the owner makes "
        "a cron job or goal you wrote his own).",
        param_model=ProposeAction,
    )
    async def propose_action(params: ProposeAction, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        words = (params.command or "").split()
        from core.surfaces import cards
        try:
            card = cards.proposal_card(user_id, words[0] if words else "", words[1:],
                                       why=params.why,
                                       session_id=_session_id(controller, execution_context)
                                       or None)
        except ValueError as e:
            return ActionResult(error=f"propose_action: {e}", include_in_memory=True)
        outcome = await _deliver(controller, user_id, card,
                                 _session_id(controller, execution_context))
        where = ("on the owner's chat" if outcome in ("sent", "queued")
                 else "in the owner's /cards list (no live chat reached)")
        return ActionResult(
            extracted_content=(f"Proposed card {card.card_id} is {where}: {card.line}. "
                               f"NOTHING was sent or spent. The owner taps 'Get a quote', then "
                               f"Confirm. Do not say it is done, queued or pending approval."),
            include_in_memory=True)
