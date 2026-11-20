"""Action cards on Telegram: ``/cards``, the ``/card_<id>_<act>`` taps, the
quote → card wrap, and the in-place edit (``core.surfaces.cards``).

A tap reaches here as the presser TYPING the token (``surfaces/telegram/
actions.py``), after the allowlist, the dedup and the tier. This module adds
the owner gate and the reads (``/cards``, ``/card_<id>``); a press is the ONE
press every owner seat shares (``surfaces/card_press.py``), with this seat's
executor as the way a card's line runs — the same room refusal, owner gate,
background rule and money gates as a typed line. A card adds no authority.

Then the card is edited in place, so a decided card has no live buttons.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.surfaces import card_groups  # noqa: F401 — installs the 084 settle hook
from core.surfaces import cards
from core.surfaces.command_reply import CommandReply, reply_text

logger = logging.getLogger(__name__)

USAGE = ("Usage: /cards — the open cards\n"
         "       /cards <id> — one card in full (the tap /card_<id>)\n"
         "       /cards <id> confirm|cancel|quote|1-6 — the same as tapping the button\n"
         "       /cancel <id> — cancel one card; /cancel cards — cancel every open card")


def _read_id(text: str) -> Optional[str]:
    """The card id of a READ tap ``/card_<id>`` (096 C4), else None."""
    from core.surfaces.tappable import parse_tappable
    verb, arg = parse_tappable(text)
    return arg if verb == "/cards" else None


def is_card_command(text: str) -> bool:
    token = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
    return (token == "/cards" or cards.parse_card_token(token)[0] is not None
            or _read_id(token) is not None)


def typed_card_tap(result: Any) -> bool:
    """126: a typed ``/approve|/reject <call card id>`` becomes that card's tap
    (``surfaces/card_press.call_card_tap``), so it is the SAME press as the button —
    a Confirm runs off the poll loop. True when the line was rewritten."""
    from surfaces.card_press import call_card_tap
    tap = call_card_tap(result.inbound.identity.user_id, result.inbound.text)
    if tap is None:
        return False
    result.inbound.text = tap
    return True


async def handle(task_agent: Any, result: Any, spawn=None, deliver=None,
                 fetch_media=None) -> Optional[Any]:
    """``/cards`` and every card tap. Returns the reply (or None)."""
    from surfaces.telegram import harness as h
    user_id = result.inbound.identity.user_id
    if not h._is_admin_owner(user_id):
        return "🔒 Owner only."
    text = (result.inbound.text or "").strip()
    read = _read_id(text)
    if read is not None:
        return cards.read_reply(user_id, read)     # a read: nothing is pressed
    cid, act = cards.parse_card_token(text)
    if cid is None:
        args = text.split()[1:]
        if not args:
            from core.surfaces.card_list import list_reply
            return list_reply(user_id)
        if len(args) == 1:
            return cards.read_reply(user_id, args[0])
        if len(args) != 2 or cards.parse_act(args[1]) is None:
            return USAGE
        cid, act = args[0].lower(), cards.parse_act(args[1])
    from surfaces.card_press import PressSeat, press
    from tools.controller.call_card_run import seat_of
    src = getattr(getattr(result.inbound, "identity", None), "source", None)

    async def _run_line(line: str, line_deliver):
        # The line runs EXACTLY as if the owner typed it: the same room refusal,
        # owner gate, background rule and money gates underneath.
        result.inbound.text = line
        result.decision.command = line.split()[0]
        return await h._handle_command(task_agent, result, spawn, line_deliver, fetch_media)

    seat = PressSeat(
        seat=seat_of(getattr(src, "surface_id", "")), user_id=user_id,
        container=getattr(task_agent, "container", None), run_line=_run_line,
        deliver=deliver,
        record=lambda text: record_out(task_agent, result, "/cards", text),
        line_records=True,
        edits_in_place=getattr(src, "surface_id", "") == "telegram")
    return await press(seat, cid, act)


def thread_line(text: str, user_id: str) -> str:
    """The owner-thread words for a card tap: the line it stands for (079 R5)."""
    return cards.tap_line(text, user_id)


def _records_out(cmd: str) -> bool:
    """A money verb (a ``core.verbs`` row with a background word), a confirmable
    verb, or a card answer: its reply is a line of the owner's conversation."""
    if cmd == "/cards" or cmd in cards.CONFIRMABLE_VERBS:
        return True
    from core.verbs import background_word
    return bool(background_word(cmd))


def record_verb(task_agent: Any, result: Any, cmd: str, text: str = "", *,
                failed: bool = False) -> None:
    """116 W3.7: one ``owner_command`` event for an owner verb (and the last
    `/status` text per seat). ``/status`` left no row and no log line. Never raises."""
    try:
        from core.runtime_paths import container_data_home
        from core.surfaces.owner_verb_log import record_owner_command
        from surfaces.telegram.status_ops import _seat_via
        record_owner_command(
            result.inbound.identity.user_id, cmd, _seat_via(result), text, failed=failed,
            session_id=getattr(result.decision, "session_id", None) or "",
            data_dir=container_data_home(getattr(task_agent, "container", None)))
    except Exception:
        logger.debug("owner verb %s: event skipped (fail-open)", cmd, exc_info=True)


def record_out(task_agent: Any, result: Any, cmd: str, reply: Any) -> Any:
    """079 R5: write a money-verb or card reply into the ONE owner thread as an
    "out" row (``source="owner_verb"``), and log the outcome once. The agent
    read only the inbound line before, so it never knew a `/bridge … go` had
    been refused or had landed. Returns ``reply`` unchanged; never raises."""
    try:
        text = reply_text(reply).strip()
        record_verb(task_agent, result, cmd, text)       # 116 W3.7: EVERY verb
        if not text or not _records_out(cmd):
            return reply
        outcome = ("refused" if cards.refused_quote(text)
                   else "failed" if cards.failed_result(text) else "ok")
        logger.info("owner verb %s: outcome=%s%s", cmd, outcome,
                    "" if outcome == "ok" else f" why={_why_line(text)!r}")
        from surfaces.telegram import harness as h
        user_id = result.inbound.identity.user_id
        if not h._is_admin_owner(user_id):
            return reply
        from core.surfaces.owner_thread import record_owner_out
        from surfaces.telegram.status_ops import _seat_via
        record_owner_out(getattr(task_agent, "container", None), user_id, text,
                         via=_seat_via(result),
                         session_id=getattr(result.decision, "session_id", None) or "",
                         source="owner_verb")
    except Exception:
        logger.debug("owner verb %s: out row skipped (fail-open)", cmd, exc_info=True)
    return reply


_WHY_MARKS = ("NOT VERIFIED", "refused", "NOT SENT", "cap", "❌", "Nothing was")


def _why_line(text: str) -> str:
    """084 WS6: the line that says WHY a money verb did not happen, for the log
    (the journal read only ``outcome=failed`` on 2026-10-19)."""
    for ln in (text or "").splitlines():
        if any(m in ln for m in _WHY_MARKS):
            return ln.strip()[:160]  # cut: a log line, not owner text
    return (text or "").strip().splitlines()[0][:160] if (text or "").strip() else ""  # cut: a log line, not owner text


def carded(user_id: str, cmd: str, text: str, reply: Any) -> Any:
    """A confirmable verb's quote → the same words as a card with buttons.

    Anything else (an error, a usage text, an executed ``go``) comes back
    unchanged."""
    if cmd not in cards.CONFIRMABLE_VERBS or not isinstance(reply, str):
        return reply
    args = (text or "").split()[1:]
    if not args and cmd in cards.FORM_VERBS:
        return builder_reply(user_id, cmd, reply)
    if (not args and cmd not in cards.BARE_CONFIRMABLE_VERBS) or cards.executes(args):
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


def _decision_target(token: str) -> Optional[str]:
    """What a DECISION tap decides (an approval item, an ask, a card), or None for a
    navigation tap (a menu button, a bare verb), whose buttons stay usable."""
    from core.surfaces.tappable import parse_ask_option, parse_tappable
    verb, arg = parse_tappable(token)
    if verb in ("/approve", "/reject"):
        return f"item:{arg}"
    ask, _letter = parse_ask_option(token)
    if ask:
        return f"ask:{ask}"
    cid, act = cards.parse_card_token(token)
    if cid and act:
        return f"card:{cid}"
    return None


def _remaining_rows(markup: Any, target: str) -> list:
    """The keyboard rows without every button that decides ``target``."""
    rows = (markup or {}).get("inline_keyboard") if isinstance(markup, dict) else None
    out = []
    for row in rows or ():
        keep = [b for b in row if isinstance(b, dict)
                and _decision_target(str(b.get("callback_data") or "")) != target]
        if keep:
            out.append(keep)
    return out


async def retire_tapped_buttons(bot: Any, update: Any, reply: str) -> bool:
    """After a DECISION tap, remove the buttons of the item it decided from the
    message it was pressed on; the other items keep theirs (2026-11-20 UX review M1).
    A navigation tap (a menu, a picker, a list) keeps every button. True when an
    edit was sent. Fail-open."""
    msg = (update or {}).get("message") if isinstance(update, dict) else None
    if not isinstance(msg, dict) or msg.get("callback_of") is None or bot is None:
        return False
    if (reply or "").lstrip().startswith(_KEEP_BUTTONS):
        return False
    target = _decision_target(str(msg.get("text") or "").strip())
    if target is None:
        return False
    chat = (msg.get("chat") or {}).get("id")
    edit = getattr(bot, "edit_message_reply_markup", None)
    if chat is None or not callable(edit):
        return False
    rows = _remaining_rows(msg.get("callback_markup"), target)
    try:
        markup = None
        if rows:
            try:
                from aiogram.types import InlineKeyboardMarkup
                markup = InlineKeyboardMarkup(inline_keyboard=rows)
            except Exception:   # no aiogram (tests): the raw Bot API shape
                markup = {"inline_keyboard": rows}
        await edit(chat_id=chat, message_id=int(msg["callback_of"]), reply_markup=markup)
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
        if cards.card_actions(card):          # the same rule as the first send
            from core.surfaces.card_text import without_taps
            source = without_taps(source)
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
