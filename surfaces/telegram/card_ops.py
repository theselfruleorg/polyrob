"""Action cards on Telegram: ``/cards``, the ``/card_<id>_<act>`` taps, the
quote → card wrap, and the in-place edit (``core.surfaces.cards``).

A tap reaches here as the presser TYPING the token (``surfaces/telegram/
actions.py``), after the allowlist, the dedup and the tier. This module adds
the owner gate and then does one of two things:

* the card answers by itself (cancel, a choice pick, an expired card);
* the card names a command line — the confirm line its quote printed, or the
  quote line of a refresh — and this seat runs it EXACTLY as if the owner had
  typed it: the same room refusal, the same owner gate, the same background
  rule, the same money gates underneath. A card adds no authority; it only
  saves re-typing a line the owner already saw.

Then the card is edited in place, so a decided card has no live buttons.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.surfaces import cards
from core.surfaces.command_reply import CommandReply, reply_text

logger = logging.getLogger(__name__)

USAGE = ("Usage: /cards — the open cards\n"
         "       /cards <id> ok|no|re|1-6 — the same as tapping the button")


def is_card_command(text: str) -> bool:
    token = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
    return token == "/cards" or cards.parse_card_token(token)[0] is not None


async def handle(task_agent: Any, result: Any, spawn=None, deliver=None,
                 fetch_media=None) -> Optional[Any]:
    """``/cards`` and every card tap. Returns the reply (or None)."""
    from surfaces.telegram import harness as h
    user_id = result.inbound.identity.user_id
    if not h._is_admin_owner(user_id):
        return "🔒 Owner only."
    text = (result.inbound.text or "").strip()
    cid, act = cards.parse_card_token(text)
    if cid is None:
        args = text.split()[1:]
        if not args:
            return cards.list_reply(user_id)
        if len(args) != 2 or args[1].lower() not in cards.ACTS:
            return USAGE
        cid, act = args[0].lower(), args[1].lower()
    p = cards.press(cid, act, user_id)
    if p.pick is not None and p.card is not None:
        from surfaces.card_builder import advance
        step = advance(p.card, p.pick, user_id)
        if not step.run:
            # Telegram edits the builder message in place (the card listener),
            # so a new message would only repeat it. Other seats print it.
            src = getattr(getattr(result.inbound, "identity", None), "source", None)
            if (getattr(src, "surface_id", "") == "telegram" and step.card is not None
                    and step.text == cards.render_text(step.card)):
                return None     # advanced: the edited card IS the answer
            return step.text
        p.run, act = step.run, "re"      # the last pick: run the verb's QUOTE
    if not p.run:
        return p.reply
    card = p.card
    verb = p.run.split()[0]
    result.inbound.text = p.run
    result.decision.command = verb
    if act != "ok":
        # A refresh, or a proposal's quote: the verb quotes and the owner-verb
        # branch turns the quote into a fresh card.
        return await h._handle_command(task_agent, result, spawn, deliver, fetch_media)

    background = deliver is not None and h._runs_in_background(verb, result)

    async def _deliver_and_finish(out: str) -> None:
        cards.finish(card.card_id, out)
        await deliver(out)

    try:
        reply = await h._handle_command(task_agent, result, spawn,
                                        _deliver_and_finish if background else deliver,
                                        fetch_media)
    except Exception as e:
        logger.error("card %s: the confirm line raised: %s", card.card_id, e, exc_info=True)
        cards.finish(card.card_id, f"Command failed: {str(e)[:200]}")
        raise
    if not background:
        cards.finish(card.card_id, reply_text(reply))
    return reply


def carded(user_id: str, cmd: str, text: str, reply: Any) -> Any:
    """A confirmable verb's quote → the same words as a card with buttons.

    Anything else (an error, a usage text, an executed ``go``) comes back
    unchanged."""
    if cmd not in cards.CONFIRMABLE_VERBS or not isinstance(reply, str):
        return reply
    args = (text or "").split()[1:]
    if not args and cmd in cards.FORM_VERBS:
        return builder_reply(user_id, cmd, reply)
    if not args or cards.executes(args):
        return reply
    new_text, card = cards.quote_card(user_id, cmd, args, reply)
    if card is None:
        return reply
    return CommandReply(new_text, card_id=card.card_id)


def builder_reply(user_id: str, verb: str, fallback: Any) -> Any:
    """A bare ``/send`` or ``/swap``: the button builder card, or the usage text
    when there is no wallet to build from."""
    try:
        from surfaces.card_builder import start
        card = start(user_id, verb)
    except Exception:
        logger.warning("builder card not started", exc_info=True)
        card = None
    if card is None:
        return fallback
    return CommandReply(cards.render_text(card), card_id=card.card_id)


def reply_actions(reply: Any) -> list:
    """The buttons of a carded command reply (none for anything else)."""
    cid = getattr(reply, "card_id", None) if isinstance(reply, CommandReply) else None
    if not cid:
        return []
    try:
        card = cards.store().get(cid)
        return cards.card_actions(card) if card is not None else []
    except Exception:
        logger.debug("card actions unavailable (fail-open)", exc_info=True)
        return []


def record_ref(reply: Any, chat_id: Any, message_id: Any) -> None:
    cid = getattr(reply, "card_id", None) if isinstance(reply, CommandReply) else None
    if not cid or message_id is None:
        return
    try:
        cards.store().add_ref(cid, "telegram", chat_id, message_id)
    except Exception:
        logger.debug("card ref not stored (fail-open)", exc_info=True)


#: A reply that means the tap did NOT act, so its buttons stay: a non-owner's
#: press (never let a stranger strip the owner's buttons), or a crash.
_KEEP_BUTTONS = ("🔒", "Command failed", "Unknown command")


async def retire_tapped_buttons(bot: Any, update: Any, reply: str) -> bool:
    """Remove the inline keyboard from the message a button was pressed on,
    once the press was handled. True when an edit was sent. Fail-open."""
    msg = (update or {}).get("message") if isinstance(update, dict) else None
    if not isinstance(msg, dict) or msg.get("callback_of") is None or bot is None:
        return False
    if (reply or "").lstrip().startswith(_KEEP_BUTTONS):
        return False
    chat = (msg.get("chat") or {}).get("id")
    edit = getattr(bot, "edit_message_reply_markup", None)
    if chat is None or not callable(edit):
        return False
    try:
        await edit(chat_id=chat, message_id=int(msg["callback_of"]), reply_markup=None)
        return True
    except Exception as e:   # "message is not modified", a deleted message
        logger.debug("telegram: tapped buttons not retired: %s", e)
        return False


def install_editor(bot: Any) -> None:
    """Edit every Telegram message that shows a card when the card changes."""
    if bot is None:
        return

    async def _edit(card: cards.Card) -> None:
        from core.surfaces.rendering import render_for_flavor, split_for_flavor
        from surfaces.telegram.actions import reply_markup_for
        source = cards.render_text(card)
        # TG10: the first send renders markdown as Telegram HTML; the edit sent
        # the raw markdown with no parse_mode, so the owner saw backticks and
        # asterisks. One message, so only the first chunk; the plain retry
        # uses the SAME splitter's source chunk (a markup rejection).
        html = render_for_flavor(source, "html", 4000)[0]
        plain = split_for_flavor(source, "html", 4000)[0]
        markup = reply_markup_for(cards.card_actions(card))
        try:
            refs = cards.store().refs(card.card_id)
        except Exception:
            return
        for surface, chat_id, message_id in refs:
            if surface != "telegram":
                continue
            cid = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
            try:
                await bot.edit_message_text(text=html, chat_id=cid,
                                            message_id=int(message_id),
                                            reply_markup=markup, parse_mode="HTML")
            except Exception as e:  # "message is not modified", a deleted message
                if "parse entities" not in str(e).lower():
                    logger.debug("telegram: card %s edit skipped: %s", card.card_id, e)
                    continue
                try:
                    await bot.edit_message_text(text=plain, chat_id=cid,
                                                message_id=int(message_id),
                                                reply_markup=markup)
                except Exception as e2:
                    logger.debug("telegram: card %s plain edit skipped: %s",
                                 card.card_id, e2)

    # One editor per process: a restarted harness replaces the old bot.
    for fn in list(cards._LISTENERS):
        if getattr(fn, "_telegram_card_editor", False):
            cards.remove_listener(fn)
    _edit._telegram_card_editor = True  # type: ignore[attr-defined]
    cards.add_listener(_edit)
