"""Action cards from the agent: ``ask_choice`` and ``propose_action``.

Before these (crypto-UX review, 2026-09-27) the agent could put a question to
the owner only as prose, and a money action only as a sentence the owner had to
turn into a typed command. Both are now cards (``core/surfaces/cards.py``):

* ``ask_choice`` — the live half of ``owner_ask(question, options)``: 2 to 6
  options as buttons on Telegram, numbered in the terminal. It WAITS for the pick and returns
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
``owner_ask``. ``owner_ask`` never calls ``ask_choice`` in an autonomous run: the
run cannot wait for a person, so it raises the durable ask instead.
"""
import asyncio
import logging
import re
import time
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

#: How long ``ask_choice`` waits by default, and at most.
DEFAULT_WAIT_S = 300
MAX_WAIT_S = 900
#: Headroom over the wait for delivery, the expiry transition and the result.
_TIMEOUT_GRACE_S = 30


def choice_action_timeout(params, base: float) -> float:
    """The controller's timeout for one ``owner_ask`` call: with options, never
    less than the wait it declared (the generic 180 s cut a 300 s wait, 2026-10-15)."""
    if not (params or {}).get("options"):
        return float(base)
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
        from core.instance import is_owner_uid
        if not is_owner_uid(user_id):
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
    try:
        return await cards.push(getattr(controller, "container", None), user_id, card,
                                session_id=session_id or None)
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


class _CardStoreUnreadable(Exception):
    """The /send repeat guards could not read the card store: they refuse, never pass."""


#: What ``propose_action`` says when a repeat guard cannot read the card store.
STORE_UNREADABLE = ("propose_action: the card store is unreadable, so I cannot check for "
                    "an earlier payment to this address. Nothing was proposed.")


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
    except Exception as e:
        logger.warning("/send repeat guard: open cards unreadable: %s", e, exc_info=True)
        raise _CardStoreUnreadable() from e
    return [c for c in open_ if c.kind == cards.KIND_PROPOSAL and c.verb == "/send"
            and _send_recipient(c.args)
            and same_address(_hex_fold(_send_recipient(c.args)), _hex_fold(to))]


#: A /send of the same token to the same address that finished inside this window
#: is "already paid": a proposal repeating it needs ``also_owed``. (Prod 2026-11-04:
#: four round winners already paid got a second card each in a re-proposed batch.)
_PAID_WINDOW_S = 72 * 3600.0


def _send_asset(args) -> tuple:
    """``(chain, token)`` of /send arguments: the chain's gas symbol reads as ``native``,
    so ``/send 1 eth`` repeats ``/send 1 native``. The same word on another chain differs."""
    low = [str(a).lower() for a in args]
    chain = low[low.index("on") + 1] if "on" in low[:-1] else ""
    token = low[1] if len(low) > 1 else ""
    try:
        from tools.defi.send_order import _native_on
        if chain and _native_on(chain, token)[0]:
            token = "native"
    except Exception as e:
        logger.debug("native check skipped for /send repeat guard: %s", e)
    return chain, token


def _hex_fold(word: str) -> str:
    """An 0x-hex word in one form (``0X``/mixed case -> lower ``0x``); any other word
    (a base58 Solana address is case-SENSITIVE) unchanged."""
    w = str(word)
    if w[:2] in ("0x", "0X") and all(c in "0123456789abcdefABCDEF" for c in w[2:]) and w[2:]:
        return "0x" + w[2:].lower()
    return w


def _send_key(args) -> tuple:
    """What a /send MEANS: ``(chain, token, recipient)`` through the seat's own parser
    (``tools.defi.send_order.parse``: free word order, ``max``/``go`` words, the gas
    symbol or its sentinel = ``native``), with 0x-hex folded. No network. Arguments
    the parser refuses fall back to the positional reading."""
    folded = [_hex_fold(a) for a in args]
    try:
        from tools.defi.send_order import parse
        order, err = parse(folded)
    except Exception as e:
        logger.debug("send parser unavailable for the /send repeat guard: %s", e)
        order, err = None, "unavailable"
    if order is not None and not err:
        return (str(order["chain"]).lower(), _hex_fold(order["token"]),
                _hex_fold(order["to"]))
    chain, token = _send_asset(folded)
    return chain, _hex_fold(token), _hex_fold(_send_recipient(folded) or "")


def _paid_recently(cards, user_id: str, words, now: Optional[float] = None) -> list:
    """/send cards of the same asset to the same address, done, running or of an
    unknown outcome, inside ``_PAID_WINDOW_S``."""
    if not words or words[0].lower() != "/send" or len(words) < 3:
        return []
    to = _send_recipient(words[1:])
    if not to:
        return []
    import time as _time
    try:
        recent = cards.store().recent_sends(
            user_id, (now if now is not None else _time.time()) - _PAID_WINDOW_S)
    except Exception as e:
        logger.warning("/send repeat guard: recent sends unreadable: %s", e, exc_info=True)
        raise _CardStoreUnreadable() from e
    key = _send_key(words[1:])
    return [r for r in recent if _send_key(r[3]) == key]


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


#: The spend-grade price a /send card is valued at when it is proposed (None =
#: ``tools.defi.price_sources.spend_price``, the send rail's own price).
_SEND_PRICE_FN = None
#: How long a proposal waits for that price; past it the card says "not priced".
_SEND_PRICE_TIMEOUT_S = 15.0
#: A dollar figure in ``why`` this many times off the command's value is flagged.
_MISMATCH_FACTOR = 10.0
_WHY_DOLLARS = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


def _why_dollars(why: str) -> List[float]:
    out = []
    for m in _WHY_DOLLARS.finditer(why or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


async def _send_value(words, why: str) -> Tuple[Optional[str], str]:
    """``(refusal, card lines)`` for a /send proposal: the token amount AND its
    USD value at the spend-grade price, read NOW, so "22 tokens = $0.05" is
    visible before the tap (prod 2026-10-28). A ``$`` amount is converted by the
    ONE parser's converter (``send_order.resolve_usd``); with no spend-grade
    price it is refused — the card could never quote. A ``why`` whose dollar
    figures are all >10x off the value gets a warning line, never a refusal."""
    if not words or words[0].lower() != "/send":
        return None, ""
    from tools.defi import send_order
    order, err = send_order.parse(words[1:])
    if err or order is None:
        return None, ""
    try:
        px = await asyncio.wait_for(
            asyncio.to_thread(send_order.unit_price, order["chain"], order["token"],
                              _SEND_PRICE_FN), timeout=_SEND_PRICE_TIMEOUT_S)
    except Exception as e:   # a slow or failed read: the quote prices it on the tap
        logger.info("propose_action: /send not priced at proposal time: %s", e)
        if order.get("usd") is not None:
            return None, (f"Value: ${order['usd']:,.2f} in tokens — the price could not "
                          f"be read now; the quote converts it and shows the count.")
        return None, (f"Value: {send_order.fmt_tokens(order['amount'])} tokens — not priced "
                      f"now; the quote shows the value.")
    # The card body is frozen text that a /cards list shows for up to a day, so
    # it names WHEN the price was read, never "now".
    read_at = time.strftime("%H:%MZ", time.gmtime())
    if order.get("usd") is not None:
        if px is None:
            return send_order.NO_PRICE_FOR_USD.format(usd=order["usd"]), ""
        priced, _, why_not = await asyncio.to_thread(
            send_order.resolve_usd, order, lambda c, a: px)
        if priced is None:
            return why_not, ""
        order = priced
        value = order["usd"]
        line = (f"Value: ${value:,.2f} = {send_order.fmt_tokens(order['amount'])} tokens "
                f"at {send_order.fmt_price(px)} (spend-grade, read {read_at}; the quote "
                f"converts again at its own price).")
    elif px is None:
        line = (f"Value: {send_order.fmt_tokens(order['amount'])} tokens — NO "
                f"spend-grade price, so the USD value is not verified.")
        stated = _why_dollars(why)
        if stated:   # review L3: the micro-cap case is exactly where this bites
            line += (f"\n⚠️ MISMATCH RISK: the reason names ${max(stated):,.2f}, but this "
                     f"command is a TOKEN count ({send_order.fmt_tokens(order['amount'])}) "
                     f"with no verified price — check the count before you confirm.")
        return None, line
    else:
        value = order["amount"] * px
        line = (f"Value: {send_order.fmt_tokens(order['amount'])} tokens = "
                f"{send_order.fmt_price(value) if value > 0 else '$0.00'} "
                f"at {send_order.fmt_price(px)} (spend-grade, read {read_at}).")
    stated = _why_dollars(why)
    if stated and value > 0 and min(max(d, value) / min(d, value)
                                     for d in stated) > _MISMATCH_FACTOR:
        line += (f"\n⚠️ MISMATCH: the reason names ${max(stated):,.2f} but this command "
                 f"is worth {send_order.fmt_price(value)}. A bare number is a TOKEN count — for dollars "
                 f"write `$` (e.g. `/send $22 …`).")
    return None, line


async def ask_choice(controller, execution_context, user_id: str, question: str,
                     options: List[str], *, why: str = "",
                     wait_seconds: int = DEFAULT_WAIT_S) -> Optional[ActionResult]:
    """The live half of ``owner_ask(options=…)``: a card, and WAIT for the pick in this
    turn. ``None`` when the card cannot reach the owner: the caller raises the durable
    ask instead, so the question is never lost."""
    why_not = _refusal(controller, execution_context, user_id)
    if why_not:
        return ActionResult(error=why_not, include_in_memory=True)
    sid = _session_id(controller, execution_context)
    from core.surfaces import cards
    try:
        card = cards.choice_card(user_id, question, list(options), body=why,
                                 session_id=sid or None, ttl=wait_seconds + 60)
    except ValueError as e:
        return ActionResult(error=f"owner_ask: {e}", include_in_memory=True)
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
        return None
    got = await asyncio.to_thread(cards.wait_for_answer, card.card_id, float(wait_seconds))
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
        extracted_content=(f"No pick within {wait_seconds}s (the card is closed). "
                           f"Do not assume an answer; continue with the safe reading or "
                           f"ask again later."),
        include_in_memory=True)


def register_card_actions(controller) -> None:
    class ProposeAction(BaseModel):
        command: str = Field(..., min_length=3, max_length=600, description=(
            "The owner money verb and its arguments, WITHOUT `go`, exactly as the owner "
            "would type it, e.g. `/send 0.5 native to 0xAbc… on base`. A bare /send "
            "amount is a TOKEN count; for a dollar amount write `$`: `/send $22 0xToken… "
            "to 0xAbc… on base` (the tool converts at the spend-grade price — never "
            "convert yourself). `/swap 0.1 native "
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

    class CardsRead(BaseModel):
        hours: float = Field(24.0, ge=1, le=168, description=(
            "How far back to list the decided cards (default 24 h)."))

    @controller.registry.action(
        "Read YOUR action cards from the card store: every open card (id, exact "
        "command, reason) and every card decided in the last hours with its outcome "
        "(sent, cancelled, replaced, expired). Use it before you tell the owner what "
        "is open, paid or owed, and before you raise or replace a card. Nothing changes.",
        param_model=CardsRead,
    )
    async def cards_read(params: CardsRead, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        from core.surfaces.card_list import agent_view
        text = await asyncio.to_thread(agent_view, user_id, hours=params.hours)
        return ActionResult(extracted_content=text, include_in_memory=True)

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
        refused, value_line = await _send_value(words, params.why)
        if refused:
            return ActionResult(error=(f"propose_action: this /send could not quote, so "
                                       f"no card was made: {refused}"), include_in_memory=True)
        from core.surfaces import cards
        try:
            clash = _open_sends_to(cards, user_id, words)
            paid = _paid_recently(cards, user_id, words)
        except _CardStoreUnreadable:
            return ActionResult(error=STORE_UNREADABLE, include_in_memory=True)
        unlisted = [c for c in clash if c.card_id not in params.replaces]
        if unlisted and not params.also_owed:
            listed = "; ".join(f"{c.card_id} ({c.line})" for c in unlisted)
            return ActionResult(error=(
                f"An open card already pays this address: {listed}. Pass "
                f"replaces=[<id>] to supersede it, or also_owed=true if BOTH payments "
                f"are owed. Nothing was proposed."), include_in_memory=True)
        if paid and not params.also_owed:
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(paid[0][2], _dt.timezone.utc).strftime("%m-%d %H:%MZ")
            unknown = sum(1 for r in paid if r[4] == "unknown")
            how = ("a confirmed /send" if not unknown else
                   f"a /send whose outcome is unknown ({unknown} of them: it may have "
                   f"landed, check /book)")
            then = ("If /book shows that it did not land, pass also_owed=true; otherwise "
                    "pass it only if ANOTHER payment is really owed." if unknown else
                    "Pass also_owed=true only if ANOTHER payment is really owed.")
            return ActionResult(error=(
                f"This address was already paid the same token by {how} "
                f"({len(paid)} in the last 72 h, the latest {when}). Nothing was "
                f"proposed. {then}"), include_in_memory=True)
        sid = _session_id(controller, execution_context)
        group_id = _group_for(user_id, sid)
        try:
            body = (value_line + "\n" + params.why).strip() if value_line else params.why
            card = cards.proposal_card(user_id, words[0] if words else "", words[1:],
                                       why=body, session_id=sid or None,
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
                               + (f"{value_line} " if value_line else "")
                               + f"NOTHING was sent or spent. The owner taps 'Get a quote', then "
                               f"Confirm. Do not say it is done, queued or pending approval."
                               + (" When the owner has decided every card you raised, you are "
                                  "woken with each card's outcome — do not ask the owner to "
                                  "tell you." if group_id else "")
                               + (f" Replaced (closed): {', '.join(replaced)}." if replaced else "")),
            include_in_memory=True)
