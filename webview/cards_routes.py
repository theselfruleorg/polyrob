"""Inbox › Cards: the owner's open action cards, and a tap on one.

``GET  /api/webgate/cards``               — the open cards (``core.surfaces.cards``)
``POST /api/webgate/cards/{card_id}/{act}`` — ok | no | re | 1-6

A tap here is the owner typing ``/card_<id>_<act>`` into the console chat box:
it runs through ``webview.console_commands.run_console_line`` → the shared
owner-verb plane (``surfaces.telegram.harness._handle_command``), so the owner
gate, the room refusal and every money gate are the SAME ones Telegram and the
REPL apply. This module carries no policy and builds no owner context.

Contributed to the Inbox destination (``webview.contributions``).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_CARD_PRESS
from webview import webgate
from webview.audit import console_write
from webview.copy import t

router = APIRouter()


def card_view(card) -> dict:
    """One card as the page draws it: its text (every token in it) and its
    buttons in the card's CURRENT state."""
    from core.surfaces import cards
    buttons = []
    for a in cards.card_actions(card):
        _cid, act = cards.parse_card_token(a.command)
        buttons.append({"label": a.label, "act": act,
                        "primary": a.style == "primary", "danger": a.style == "danger"})
    return {"id": card.card_id, "kind": card.kind, "origin": card.origin,
            "state": card.state, "title": card.title,
            "text": cards.render_text(card), "buttons": buttons}


@router.get("/api/webgate/cards")
async def api_cards(request: Request):
    """The owner's open cards, newest first. An unreadable store is NAMED
    (``readable: false``), never an empty list. A READ: it never sweeps stuck
    cards (that writes and notifies — ``/cards`` in chat does it), and the
    open list it shows holds only ``open`` cards anyway (audit WR11)."""
    from core.surfaces import cards
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    try:
        open_ = cards.store().open_cards(str(uid))
    except Exception as e:
        return JSONResponse({"readable": False, "reason": type(e).__name__,
                             "cards": [], "user_id": uid})
    return JSONResponse({"readable": True, "cards": [card_view(c) for c in open_],
                         "user_id": uid})


@router.get("/api/webgate/cards/pickers")
async def api_card_pickers(request: Request):
    """What the send/swap form offers to pick: the chains where value may move,
    the tokens Rob trusts (by chain), and the addresses earlier confirmed sends
    went to. A source that cannot be read is an empty list plus its name in
    ``unreadable`` — never a silent gap."""
    from core.surfaces import cards
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    out = {"chains": [], "swap_chains": [], "tokens": [], "recipients": [],
           "unreadable": []}
    try:
        from surfaces.card_builder import chain_choices
        out["chains"] = chain_choices("/send")
        out["swap_chains"] = chain_choices("/swap")
    except Exception:
        out["unreadable"].append("chains")
    try:
        from core.wallet.token_trust import trust_view
        from webview.tokens_routes import _positions_db
        view = trust_view(uid, positions_db=_positions_db())
        out["tokens"] = [{"chain": r.get("chain"), "address": r.get("address"),
                          "symbol": r.get("symbol") or ""}
                         for r in view.get("trusted", [])]
        # Audit WR4: a trust source trust_view could not read makes this list
        # partial — say so, never offer a short list as the whole one.
        if view.get("unreadable"):
            out["unreadable"].append("tokens")
    except Exception:
        out["unreadable"].append("tokens")
    try:
        out["recipients"] = cards.store().recent_recipients(str(uid))[:10]
    except Exception:
        out["unreadable"].append("recipients")
    return JSONResponse(out)


@router.post("/api/webgate/cards/quote", dependencies=webgate.MUTATION_DEPS)
async def api_card_quote(request: Request):
    """Build a /send or /swap QUOTE from a form and run it as the owner typing
    it. Body: ``{"verb", "amount", "token", "to", "chain", "slippage"?}``.
    Answers ``{ok, message, card}``: the quote's own words, and the card its
    Confirm lives on (null when the quote refused)."""
    from core.surfaces import cards
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    line, why = cards.form_line(str(body.get("verb") or ""), body)
    if line is None:
        return JSONResponse({"ok": False, "message": why, "card": None}, status_code=400)
    from webview.console_commands import run_console_line
    from webview.server import _in_process_task_agent
    import time
    started = time.time()
    reply = await run_console_line(_in_process_task_agent(), "money", str(uid), " ".join(line))
    # Only a card THIS quote made: an older open card for the same line must
    # never stand in for a quote that just refused.
    card = cards.newest_quote_for(str(uid), line[0], line[1:], since=started)
    console_write(CONSOLE_CARD_PRESS, user_id=uid,
                  attrs={"act": "quote", "verb": line[0], "ok": card is not None})
    return JSONResponse({"ok": card is not None,
                         "message": str(reply or "") or t("inbox.unreachable"),
                         "card": card_view(card) if card is not None else None})


@router.post("/api/webgate/cards/{card_id}/{act}", dependencies=webgate.MUTATION_DEPS)
async def api_card_press(request: Request, card_id: str, act: str):
    """One tap. Answers ``{ok, message, card}`` — the message is the seat's
    own reply, verbatim; ``card`` is the card after the tap (or null)."""
    from core.surfaces import cards
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    token = cards.card_token(str(card_id).lower(), str(act).lower())
    if cards.parse_card_token(token)[0] is None:
        return JSONResponse({"ok": False, "message": t("inbox.cards.bad_tap"), "card": None},
                            status_code=404)
    from webview.console_commands import run_console_line
    from webview.server import _in_process_task_agent
    reply = await run_console_line(_in_process_task_agent(), "inbox", str(uid), token)
    after = None
    try:
        got = cards.store().get(str(card_id).lower())
        if got is not None and str(got.user_id) == str(uid):
            after = card_view(got)
    except Exception:
        after = None
    text = str(reply or "")
    ok = bool(text) and not text.lstrip().startswith(("❌", "🔒", "No such card",
                                                       "That card"))
    console_write(CONSOLE_CARD_PRESS, user_id=uid,
                  attrs={"card_id": str(card_id), "act": str(act), "ok": ok})
    return JSONResponse({"ok": ok, "message": text or t("inbox.unreachable"),
                         "card": after})


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="inbox", source="webview.cards_routes")

__all__ = ["router", "card_view"]
