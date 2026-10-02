"""Build a /send or /swap from buttons — one field per tap (action cards).

``/send`` or ``/swap`` with no arguments answers with a BUILDER card
(``core.surfaces.cards.KIND_BUILDER``): pick the chain, then the token, then
the recipient (or the token to buy), then the amount — each a row of buttons
on the SAME card, which Telegram edits in place. The last tap runs the verb's
QUOTE exactly as if the owner had typed the line, and the quote answers with
its own quote card (Confirm / Refresh / Cancel). Nothing moves before that
Confirm.

Shared by every seat (Telegram + console via ``surfaces/telegram/card_ops.py``,
the REPL via ``cli/ui/commands/h_cards.py``). It lives in ``surfaces/`` because
the pickers read the wallet (addresses, balances) and the token trust list.

⚠️ The button carries only the option's NUMBER; the value it stands for is
stored on the card (``draft["_values"]``). A pick advances the card with a
compare-and-swap on its ``updated_at``, so two taps on one step advance once.
Every option is a value the verb's own parser checks again: a card never
widens what a typed line may do.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, List, Optional, Tuple

from core.surfaces import cards

logger = logging.getLogger(__name__)

#: The steps of each verb, in order. ``to`` is the recipient for /send and the
#: token bought for /swap.
STEPS = {"/send": ("chain", "token", "to", "amount"),
         "/swap": ("chain", "token", "to", "amount")}

_PROMPT = {
    ("/send", "chain"): "Pick the chain to send on:",
    ("/send", "token"): "Pick what to send:",
    ("/send", "to"): "Pick the recipient (earlier confirmed sends):",
    ("/send", "amount"): "Pick the amount:",
    ("/swap", "chain"): "Pick the chain to swap on:",
    ("/swap", "token"): "Pick what to sell:",
    ("/swap", "to"): "Pick what to buy:",
    ("/swap", "amount"): "Pick how much to sell:",
}

#: The share of the balance each amount button offers. Native keeps a margin
#: for the fee, so its top button is 90%, never 100%.
_TOKEN_SHARES = (25, 50, 100)
_NATIVE_SHARES = (25, 50, 90)

BUILDER_TTL_S = 30 * 60


@dataclass
class Step:
    """What a pick produced: the text to show, the card, and — on the last
    step — the quote line the seat runs as the owner typing it."""
    text: str
    card: Optional[cards.Card]
    run: Optional[str] = None


def chain_choices(verb: str) -> List[str]:
    """The chains a /send or /swap may be built on — the builder's first step
    and the console form's picker read this ONE list."""
    return _chains(verb)


def _chains(verb: str) -> List[str]:
    from core.wallet import chains
    base = chains.swap_chains() if verb == "/swap" else chains.money_chains()
    out = list(base)
    if "solana" in chains.names() and "solana" not in out:
        out.append("solana")          # its own verbs gate it (solana_transfer/swap)
    return out[:cards.MAX_OPTIONS]


def _wallet(tool: Any = None):
    try:
        if tool is None:
            from tools.defi.trade_tool import DefiTradeTool
            tool = DefiTradeTool()
        return tool._get_wallet()
    except Exception:
        logger.debug("builder: wallet unavailable", exc_info=True)
        return None


def _holder(wallet, chain: str) -> Optional[str]:
    try:
        if chain == "solana":
            return wallet.solana_address
        return wallet.operational_signer().address
    except Exception:
        return None


def _trusted(user_id: str, chain: str) -> List[Tuple[str, str]]:
    """``[(label, address)]`` — the tokens Rob trusts on ``chain``."""
    try:
        from core.open_positions import open_positions_db_path
        from core.runtime_paths import resolve_data_home
        from core.wallet.token_trust import trust_view
        view = trust_view(str(user_id),
                          positions_db=open_positions_db_path(str(resolve_data_home())))
    except Exception:
        logger.debug("builder: trust view unreadable", exc_info=True)
        return []
    out = []
    for row in view.get("trusted") or []:
        if str(row.get("chain") or "").lower() == chain and row.get("address"):
            out.append((str(row.get("symbol") or row["address"][:10]), str(row["address"])))
    return out


def _short_addr(a: str) -> str:
    return a if len(a) <= 14 else f"{a[:6]}…{a[-4:]}"


def _recipients(user_id: str, chain: str) -> List[Tuple[str, str]]:
    try:
        recent = cards.store().recent_recipients(str(user_id))
    except Exception:
        return []
    solana = chain == "solana"
    return [(_short_addr(a), a) for a in recent if a.startswith("0x") != solana]


def _balance(wallet, chain: str, token: str) -> Optional[Decimal]:
    """The wallet's balance of ``token`` in whole units, or None (unknown)."""
    holder = _holder(wallet, chain)
    if not holder:
        return None
    try:
        if chain == "solana":
            from core.wallet import solana_onchain
            if token == "native":
                v = solana_onchain.native_balance(holder)
                return None if v is None else Decimal(str(v))
            return None                    # a mint's decimals are not read here
        from core.wallet.bridge_guard import native_balance_raw, token_balance_raw
        if token == "native":
            raw = native_balance_raw(holder, chain)
            return None if raw is None else Decimal(raw) / Decimal(10 ** 18)
        raw = token_balance_raw(holder, chain, token)
        if raw is None:
            return None
        from core.wallet.tokens import get_token_identity
        dec = int(get_token_identity(chain, token).decimals)
        return Decimal(raw) / Decimal(10 ** dec)
    except Exception:
        logger.debug("builder: balance read failed", exc_info=True)
        return None


def _fmt(amount: Decimal) -> str:
    q = amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN).normalize()
    return format(q, "f")


def _amounts(wallet, chain: str, token: str) -> List[Tuple[str, str]]:
    bal = _balance(wallet, chain, token)
    if bal is None or bal <= 0:
        return []
    shares = _NATIVE_SHARES if token == "native" else _TOKEN_SHARES
    out = []
    for pct in shares:
        amt = bal * Decimal(pct) / Decimal(100)
        text = _fmt(amt)
        if Decimal(text) > 0:
            out.append((f"{pct}% · {text}", text))
    return out


def _options(verb: str, step: str, draft: dict, user_id: str,
             wallet) -> List[Tuple[str, str]]:
    chain = draft.get("chain", "")
    if step == "chain":
        return [(c, c) for c in _chains(verb)]
    if step == "token":
        return [("native", "native")] + _trusted(user_id, chain)
    if step == "to" and verb == "/send":
        return _recipients(user_id, chain)
    if step == "to":                                   # /swap: what to buy
        buy = [t for t in _trusted(user_id, chain) if t[1] != draft.get("token")]
        if chain == "solana" and draft.get("token") != "native":
            buy = [("native", "native")] + buy
        return buy
    if step == "amount":
        return _amounts(wallet, chain, draft.get("token", ""))
    return []


def _line(verb: str, draft: dict) -> str:
    """The command so far, with ``…`` for what is not picked yet."""
    return (f"{verb} {draft.get('amount', '<amount>')} {draft.get('token', '<token>')} "
            f"to {draft.get('to', '<address>' if verb == '/send' else '<token>')} "
            f"on {draft.get('chain', '<chain>')}")


def _body(verb: str, step: str, draft: dict, options: list) -> str:
    lines = [f"So far: {_line(verb, draft)}", ""]
    if options:
        lines.append(_PROMPT[(verb, step)])
    else:
        why = {"to": ("No recipients from earlier confirmed sends on this chain."
                      if verb == "/send" else "No trusted token to buy on this chain."),
               "amount": "I could not read the balance of that token.",
               "token": "No token to pick on this chain."}.get(step, "Nothing to pick.")
        lines.append(f"{why} Type the full command instead — fill in the <…> part:")
        lines.append(_line(verb, draft))
    return "\n".join(lines)


def _set_step(verb: str, draft: dict, step: str, user_id: str, wallet):
    opts = _options(verb, step, draft, user_id, wallet)[:cards.MAX_OPTIONS]
    new = dict(draft)
    new["_step"] = step
    new["_values"] = [v for _l, v in opts]
    return new, [label for label, _v in opts], _body(verb, step, new, opts)


def start(user_id: str, verb: str, *, tool: Any = None,
          st: Optional[cards.CardStore] = None) -> Optional[cards.Card]:
    """A new builder card at its first step, or None (no wallet here)."""
    verb = verb.lower()
    if verb not in STEPS:
        return None
    wallet = _wallet(tool)
    if wallet is None:
        return None
    draft, options, body = _set_step(verb, {}, STEPS[verb][0], user_id, wallet)
    card = cards._new(user_id, cards.KIND_BUILDER, cards.ORIGIN_SYSTEM,
                      f"{verb} — build it with buttons", body=body, verb=verb,
                      options=options, ttl=BUILDER_TTL_S, st=st)
    stored = (st or cards.store()).transition(card.card_id, frozenset({cards.S_OPEN}),
                                              cards.S_OPEN, draft=draft)
    return stored or card


def advance(card: cards.Card, pick: int, user_id: str, *, tool: Any = None,
            st: Optional[cards.CardStore] = None) -> Step:
    """Apply one pick. The last pick returns the quote line to run."""
    st = st or cards.store()
    verb = card.verb
    steps = STEPS.get(verb)
    draft = dict(card.draft or {})
    values = list(draft.get("_values") or [])
    step = draft.get("_step")
    if not steps or step not in steps or not (1 <= pick <= len(values)):
        return Step("That option does not exist on this card.", card)
    draft[step] = values[pick - 1]
    idx = steps.index(step)
    if idx + 1 < len(steps):
        wallet = _wallet(tool)
        new_draft, options, body = _set_step(verb, draft, steps[idx + 1], user_id, wallet)
        done = st.transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_OPEN,
                             expect_updated_at=card.updated_at, draft=new_draft,
                             options=options, body=body)
        if done is None:
            return Step("That card moved on a moment ago — use the newest buttons.",
                        st.get(card.card_id))
        cards._notify(done)
        return Step(cards.render_text(done), done)
    line = _line(verb, draft)
    done = st.transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_REPLACED,
                         expect_updated_at=card.updated_at, draft=draft,
                         result=f"Quoted: {line}")
    if done is None:
        return Step("That card moved on a moment ago — use the newest buttons.",
                    st.get(card.card_id))
    cards._notify(done)
    return Step("", done, run=line)


__all__ = ["STEPS", "Step", "advance", "start"]
