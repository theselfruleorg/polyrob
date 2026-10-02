"""Action cards in the REPL — the twin of ``surfaces/telegram/card_ops.py``.

The terminal has no buttons, so a card is its text: every action's one-token
command is printed on it, and typing that token is the tap. This module adds:

* ``/cards`` — the open cards, and ``/cards <id> <act>``;
* the folded tap tokens every seat prints — ``/card_<id>_<act>``,
  ``/approve_p_<hex>`` / ``/approve_tap_<hex>`` / ``/approve_all`` (and
  ``/reject_…``), ``/fulfill_<ask>_<letter>`` — which the REPL used to answer
  with "Unknown command";
* the quote → card wrap for the confirmable money verbs.

A card confirm dispatches the line the card carries through THIS registry, the
same as the owner typing it, so every REPL gate applies to it again.
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.surfaces import cards


def _tenant(ctx) -> str:
    from cli._admin_home import admin_owner_tenant
    return admin_owner_tenant(getattr(ctx, "user_id", None))


async def dispatch_token(registry: Any, line: str, ctx: Any) -> bool:
    """Run a folded tap token. True when ``line`` was one (handled)."""
    token = (line or "").strip().split()[0] if (line or "").strip() else ""
    cid, act = cards.parse_card_token(token)
    if cid:
        await _press(registry, cid, act, ctx)
        return True
    from core.surfaces.tappable import parse_ask_option, parse_tappable
    verb, arg = parse_tappable(token)
    if verb and arg:
        await registry.dispatch(f"{verb} {arg}", ctx)
        return True
    ask_id, letter = parse_ask_option(token)
    if ask_id:
        # The tapped option is the owner's TYPED answer: the text the ask
        # stored when it was raised, never a paraphrase (Telegram's rule).
        try:
            from cli.ui.commands.h_owner import _goal_board
            ask = _goal_board(write=None).get(ask_id)
            opts = ((ask.payload or {}).get("options") or {}) if (
                ask is not None and str(ask.user_id) == _tenant(ctx)) else {}
        except Exception:
            opts = {}
        if letter not in opts:
            ctx.emit(f"No open ask '{ask_id}' with option {letter} — see /asks.",
                     title="fulfill")
            return True
        await registry.dispatch(f"/fulfill {ask_id} {letter}) {opts[letter]}", ctx)
        return True
    return False


def _restore(wrap) -> None:
    """Put ``ctx.emit`` back exactly as it was (instance attr or class method)."""
    if wrap._had:
        wrap.ctx.emit = wrap._orig
    else:
        try:
            del wrap.ctx.emit
        except AttributeError:
            pass


class _Capture:
    """Collect what a dispatched line printed, and still print it."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.lines: List[str] = []
        self._orig = ctx.emit
        self._had = "emit" in vars(ctx)

    def __enter__(self):
        def _emit(text, *a, **kw):
            self.lines.append(str(text))
            return self._orig(text, *a, **kw)
        self.ctx.emit = _emit
        return self

    def __exit__(self, *exc):
        _restore(self)
        return False


async def _press(registry: Any, cid: str, act: str, ctx: Any) -> None:
    p = cards.press(cid, act, _tenant(ctx))
    if p.pick is not None and p.card is not None:
        from surfaces.card_builder import advance
        step = advance(p.card, p.pick, _tenant(ctx))
        if not step.run:
            ctx.emit(step.text, title="cards")
            return
        await registry.dispatch(step.run, ctx)      # the verb's QUOTE -> a quote card
        return
    if not p.run:
        ctx.emit(p.reply, title="cards")
        return
    if act != "ok":
        await registry.dispatch(p.run, ctx)
        return
    with _Capture(ctx) as cap:
        try:
            await registry.dispatch(p.run, ctx)
        except Exception as e:   # never leave the card "running"
            cards.finish(cid, f"Command failed: {str(e)[:200]}")
            raise
    cards.finish(cid, "\n".join(cap.lines))


def h_cards(ctx) -> Any:
    args = list(getattr(ctx, "args", None) or [])
    if not args:
        ctx.emit(cards.list_reply(_tenant(ctx)), title="cards")
        return None
    if len(args) != 2 or args[1].lower() not in cards.ACTS:
        ctx.emit("usage: /cards  |  /cards <id> ok|no|re|1-6", title="cards")
        return None
    registry = getattr(ctx, "registry", None)
    if registry is None:
        from cli.ui.commands.handlers import build_default_registry
        registry = build_default_registry()
    return _press(registry, args[0].lower(), args[1].lower(), ctx)


def wrap_quote(verb: str, ctx: Any) -> Optional[Any]:
    """While a confirmable verb runs, turn its quote into a card.

    Returns a context manager, or None when this line is not a quote."""
    verb = "/" + verb.lstrip("/").lower()
    args = list(getattr(ctx, "args", None) or [])
    if not args and verb in cards.FORM_VERBS:
        return _BuilderWrap(verb, [], ctx)
    if verb not in cards.CONFIRMABLE_VERBS or not args or cards.executes(args):
        return None
    return _QuoteWrap(verb, args, ctx)


class _QuoteWrap:
    def __init__(self, verb: str, args: List[str], ctx: Any):
        self.verb, self.args, self.ctx = verb, args, ctx
        self._orig = ctx.emit
        self._had = "emit" in vars(ctx)
        self._done = False

    def __enter__(self):
        def _emit(text, *a, **kw):
            if not self._done and isinstance(text, str):
                new_text, card = cards.quote_card(_tenant(self.ctx), self.verb,
                                                  self.args, text)
                if card is not None:
                    self._done = True
                    text = new_text
            return self._orig(text, *a, **kw)
        self.ctx.emit = _emit
        return self

    def __exit__(self, *exc):
        _restore(self)
        return False


class _BuilderWrap(_QuoteWrap):
    """A bare /send or /swap: its usage text becomes the button builder card."""

    def __enter__(self):
        def _emit(text, *a, **kw):
            if not self._done and isinstance(text, str):
                self._done = True
                try:
                    from surfaces.card_builder import start
                    card = start(_tenant(self.ctx), self.verb)
                except Exception:
                    card = None
                if card is not None:
                    text = cards.render_text(card)
            return self._orig(text, *a, **kw)
        self.ctx.emit = _emit
        return self


HELP_CARDS = (
    "  Action cards: one decision you take with a tap on Telegram, or by\n"
    "  typing the card's token here. A money quote becomes a card with\n"
    "  Confirm / Refresh / Cancel; the agent's proposals and questions are\n"
    "  cards too. A card is decided once — a second tap runs nothing.\n"
    "\n"
    "    /cards                    the open cards\n"
    "    /card_<id>_ok             confirm (runs the exact line the quote showed)\n"
    "    /card_<id>_re             refresh the quote (or quote a proposal)\n"
    "    /card_<id>_no             cancel\n"
    "    /card_<id>_1 … _6         pick an option",
    "`/cards` and the card buttons on Telegram; the Inbox in the console.",
)


def register(reg, Command) -> None:
    reg.register(Command(
        "cards", h_cards,
        "Open action cards: confirm a quote, answer a choice, or cancel",
        usage="[<id> ok|no|re|1-6]", group="needs you",
        help_long=HELP_CARDS[0], elsewhere=HELP_CARDS[1]))
