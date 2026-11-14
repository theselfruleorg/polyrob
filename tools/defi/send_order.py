"""The ``/send`` order: the words of a send, parsed and proven (owner verb and
``propose_action`` read the SAME parser; it lives in the tools tier so the agent's
card proposal can quote a send before it makes a card). Moved verbatim from
``surfaces/telegram/send_ops.py``, which re-exports every name."""
from __future__ import annotations

import math
import re
from typing import Callable, List, Optional, Tuple

from core.surfaces.cards import GO_WORDS as _GO

_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_NATIVE = "native"
#: The owner's extra confirmation for a token whose value is NOT VERIFIED (no
#: spend-grade price): a wallet sends any token, after one more deliberate step.
ANYWAY = "anyway"

#: A quote is re-priced at send time; the declared bound leaves this much room
#: for the price to move between the two (the bridge's own tolerance). A
#: dollar amount (``$22``) is held to the same room when no ``max`` is typed.
PRICE_ROOM = 1.05
#: The room a TOKEN-COUNT send's confirm line carries (max <quote + 25%>). The
#: count is fixed, so a move only changes its dollar value: 5% refused a thin
#: token's bounty payout "the price moved" on an 8% move (prod card
#: 6f3ea59c10, 2026-11-13), while a pump of the token still stops it. The
#: thin-pool dispute band (``tools.intel.price.DISPUTE_PCT_THIN``) is the same 25%.
TOKEN_COUNT_ROOM = 1.25

USAGE = ("Usage: /send <amount|$usd> <native|token-address> to <address> on <chain> "
         "[max <usd>] [anyway] [go]\n"
         "e.g. /send 0.5 native to 0x2FAa…f0e7 on ethereum      — quote only\n"
         "     /send 0.5 native to 0x2FAa…f0e7 on ethereum go   — send\n"
         "     /send 25 0x8335…2913 to 0xAbc…123 on base go     — 25 TOKENS\n"
         "     /send $22 0x8335…2913 to 0xAbc…123 on base       — $22 worth of the token\n"
         "     /send 1.2 sol to 7xKX…9fQ on solana go\n\n"
         "A bare number is a TOKEN count. A `$` amount (`$22`, `22usd`) is dollars: "
         "the send turns it into tokens at the spend-grade price when it runs, "
         "shows the count and the price, and is held to the dollars + 5% (or "
         "`max`). A token with no spend-grade price takes a token amount only.\n\n"
         "The quote shows the value and the caps it must fit, and a Confirm "
         "button that sends exactly that quote (at most `max` USD). `go` sends: "
         "you typed the address, so there is no second tap. Your own send is not "
         "held by /pause or the autonomous ceiling; the per-transaction and "
         "daily caps always apply. A token with no reliable price still sends: "
         "the quote asks for one extra step, `anyway`.")


def _native_on(chain: str, token: str) -> Tuple[bool, Optional[str]]:
    """``(is_native, refusal)``. ``native`` always means THIS chain's gas asset;
    a gas SYMBOL counts only when it is this chain's own (ETH on ethereum, SOL
    on solana). Another chain's symbol is refused, never mapped: until
    2026-09-27 `/send 0.5 sol … on ethereum go` sent 0.5 ETH."""
    t = (token or "").strip()
    if t.lower() == _NATIVE:
        return True, None
    if _EVM_ADDR.match(t) or (chain == "solana" and _SOL_ADDR.match(t)):
        if chain != "solana":
            try:
                from tools.defi.trade_tool import _native_send_symbol
                return _native_send_symbol(chain, t) is not None, None
            except Exception:
                return False, None
        return False, None
    if chain == "solana":
        own = "SOL"
    else:
        try:
            from core.wallet import chains
            row = chains.get(chain)
            own = str(getattr(row, "native_symbol", "") or "") if row else ""
        except Exception:
            own = ""
    if own and t.upper() == own.upper():
        return True, None
    here = f"the native asset on {chain} is {own}" if own else f"{chain} is not a known chain"
    return False, (f"{t!r} is not a token address, and not the native asset here "
                   f"({here}). Write `native`, or the token's contract address (a "
                   f"ticker is never enough). Nothing was sent.")


_USD_AMOUNT = re.compile(
    r"^(?:\$([0-9][0-9,]*(?:\.[0-9]+)?)|([0-9][0-9,]*(?:\.[0-9]+)?)usd)$", re.IGNORECASE)


def usd_amount(word: str) -> Optional[float]:
    """The dollars in ``$22`` / ``$22.50`` / ``22usd``, or None for any other word
    (a bare number is a TOKEN count, never dollars)."""
    m = _USD_AMOUNT.match((word or "").strip())
    if not m:
        return None
    try:
        return float((m.group(1) or m.group(2)).replace(",", ""))
    except ValueError:
        return None


def parse(args: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    """``(order, None)`` or ``(None, error)``. Order keys: amount, usd, token,
    to, chain, go, max_usd, anyway. A ``$`` amount sets ``usd`` and leaves
    ``amount`` None until :func:`resolve_usd` converts it at the spend-grade
    price. Word order is free apart from the ``to``/``on`` markers."""
    words = [w for w in (args or []) if w]
    go = bool(words) and words[-1].lower() in _GO
    if go:
        words = words[:-1]
    chain = to = None
    anyway = False
    max_usd: Optional[float] = None
    rest: List[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w.lower() == "max" and i + 1 < len(words):
            # The most this send may be worth in USD — the bound a quote card's
            # Confirm carries, so the send is held to the price it showed.
            try:
                max_usd = float(words[i + 1].lstrip("$").replace(",", ""))
            except ValueError:
                return None, f"`max` needs a USD number, got {words[i + 1]!r}."
            if not (max_usd > 0):
                return None, f"`max` must be above zero, got {words[i + 1]!r}."
            i += 2
            continue
        if w.lower() == ANYWAY:
            anyway = True
            i += 1
            continue
        if w.lower() == "on" and i + 1 < len(words):
            chain = words[i + 1].lower()
            i += 2
            continue
        if w.lower() == "to" and i + 1 < len(words):
            to = words[i + 1]
            i += 2
            continue
        rest.append(w)
        i += 1
    if len(rest) != 2:
        return None, USAGE
    usd = usd_amount(rest[0])
    amount: Optional[float] = None
    if usd is not None:
        if not (usd > 0):
            return None, f"A dollar amount must be above zero, got {rest[0]!r}."
    else:
        try:
            amount = float(rest[0])
        except ValueError:
            return None, f"Amount must be a positive number, got {rest[0]!r}.\n\n{USAGE}"
        if not (amount > 0) or not math.isfinite(amount):
            return None, f"Amount must be a positive number, got {rest[0]!r}."
    token = rest[1]
    if not to:
        return None, "Name the recipient: `to <address>`.\n\n" + USAGE
    if not chain:
        return None, ("Name the chain: `on <chain>` — I never guess where money "
                      "goes.\n\n" + USAGE)
    solana = chain == "solana"
    if solana and not _SOL_ADDR.match(to):
        return None, f"{to!r} is not a Solana address."
    if not solana and not _EVM_ADDR.match(to):
        return None, f"{to!r} is not an 0x address (40 hex characters)."
    if not solana and not _checksum_ok(to):
        return None, (f"{to!r} fails its checksum — the mixed upper/lower case "
                      f"does not match the address, which usually means a typo. "
                      f"Copy it again, or write it all in lower case if you are "
                      f"sure. Nothing was sent.")
    native, why = _native_on(chain, token)
    if why:
        return None, why
    if not native and not solana and not _EVM_ADDR.match(token):
        return None, (f"{token!r} is not a token address. Use `native`, or the "
                      f"token's contract address (a ticker is never enough).")
    if not native and not solana and not _checksum_ok(token):
        return None, (f"{token!r} fails its checksum (mixed case that does not "
                      f"match the address). Copy it again. Nothing was sent.")
    if not native and solana and not _SOL_ADDR.match(token):
        return None, (f"{token!r} is not a mint address. Use `native` (SOL), or "
                      f"the token's mint address (a ticker is never enough).")
    return {"amount": amount, "usd": usd, "token": "native" if native else token,
            "to": to, "chain": chain, "go": go, "max_usd": max_usd,
            "anyway": anyway}, None


# ── a dollar amount, converted by the tool (never by the model) ─────────────
#
# Prod 2026-10-28: payout cards meant as $22 were raised as `/send 22 <token>`
# and sent 22 tokens (~$0.05); the model believed the tap converts dollars. A
# `$` amount is now real: the tool turns it into tokens at the SAME spend-grade
# price the send's value check and caps use, and refuses when there is none.

def price_address(chain: str, token: str) -> Optional[str]:
    """The address a price source knows *token* by: the chain's wrapped native
    for ``native`` (what the guard prices a native send at), else the token."""
    if token != _NATIVE:
        return token
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        return None
    return (getattr(row, "wrapped_native", None) or None) if row else None


def unit_price(chain: str, token: str,
               price_fn: Optional[Callable[[str, str], Optional[float]]] = None
               ) -> Optional[float]:
    """The spend-grade USD price of ONE unit of *token* on *chain*, or None.

    The send rails' own rule: ``tool._price`` (= ``price_sources.spend_price``)
    on the token, or on the wrapped native for ``native``; Solana's pinned USDC
    is $1.00 by definition (``solana_send_verb``). A zero, negative or
    non-finite price is no price. Raises only what *price_fn* raises."""
    if chain == "solana" and token != _NATIVE:
        try:
            from core.wallet import chains
            row = chains.get("solana")
            if row is not None and getattr(row, "usdc", None) == token:
                return 1.0
        except Exception:
            pass
    addr = price_address(chain, token)
    if not addr:
        return None
    if price_fn is None:
        from tools.defi.price_sources import spend_price as price_fn
    from core.wallet.tx_guard_steps.value import _spend_price
    px = _spend_price(price_fn(chain, addr), chain)
    if px is None or not math.isfinite(px) or px <= 0:
        return None
    return px


def round_tokens(amount: float, digits: int = 6,
                 decimals: Optional[int] = None) -> float:
    """*amount* to *digits* significant digits: the count the quote shows IS
    the count that is sent (never a float tail the owner did not see).

    With the token's *decimals* known, the count never carries more places
    than the token has (cut DOWN, so the dollars are never exceeded): $5 of a
    $95,000 token with 8 decimals is 0.00005263, not 0.0000526316 — the exact
    raw conversion refused the longer count on every send."""
    if not (amount > 0) or not math.isfinite(amount):
        return amount
    places = max(0, min(18, digits - int(math.floor(math.log10(amount))) - 1))
    if decimals is not None and 0 <= int(decimals) < places:
        from decimal import ROUND_DOWN, Decimal
        q = Decimal(1).scaleb(-int(decimals))
        return float(Decimal(repr(round(amount, places))).quantize(q, rounding=ROUND_DOWN))
    return round(amount, places)


def token_decimals(chain: str, token: str) -> Optional[int]:
    """The decimals of *token* on *chain*, or None when not known without
    doubt. Native = the chain row's; Solana's pinned USDC = 6; an EVM token =
    its frozen identity read. Never raises."""
    try:
        from core.wallet import chains
        row = chains.get(chain)
        if row is None:
            return None
        if token == _NATIVE:
            return int(row.native_decimals)
        if row.family == "svm":
            return 6 if getattr(row, "usdc", None) == token else None
        from core.wallet.tokens import bounded_decimals, get_token_identity
        return bounded_decimals(get_token_identity(chain, token).decimals)
    except Exception:
        return None


def fmt_tokens(amount: float) -> str:
    """A token count for a person: grouped, no exponent, no trailing zeros."""
    if amount >= 1:
        text = f"{amount:,.6f}"
    else:
        text = f"{amount:.18f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_price(px: float) -> str:
    """A unit price with enough digits to read a micro-priced token."""
    if px >= 1:
        return f"${px:,.2f}"
    places = min(18, max(2, 3 - int(math.floor(math.log10(px)))))
    return f"${px:.{places}f}"


def implied_max(usd: float) -> float:
    """The cap a ``$`` amount carries when no ``max`` was typed."""
    return round(usd * PRICE_ROOM + 0.01, 2)


NO_PRICE_FOR_USD = ("No spend-grade price for this token, so I cannot turn "
                    "${usd:,.2f} into a token amount — I never guess a price. "
                    "Give a token amount instead (`/send <amount> <token> …`). "
                    "Nothing was sent.")


def resolve_usd(order: dict,
                price_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                decimals_fn: Optional[Callable[[str, str], Optional[int]]] = None
                ) -> Tuple[Optional[dict], Optional[float], Optional[str]]:
    """``(order, unit_price, None)`` or ``(None, None, refusal)``.

    A token-amount order comes back unchanged with ``unit_price`` None. A ``$``
    order comes back with ``amount`` = dollars / spend-grade price (rounded to
    the count it shows) and ``max_usd`` = the typed ``max`` or the dollars + the
    price room. No spend-grade price = a plain refusal, never a guess."""
    usd = order.get("usd")
    if usd is None:
        return order, None, None
    px = unit_price(order["chain"], order["token"], price_fn)
    if px is None:
        return None, None, NO_PRICE_FOR_USD.format(usd=usd)
    out = dict(order)
    dec = (decimals_fn or token_decimals)(order["chain"], order["token"])
    out["amount"] = round_tokens(usd / px, decimals=dec)
    if not out["amount"] > 0:
        return None, None, (f"${usd:,.2f} is less than one unit of this token at "
                            f"{fmt_price(px)}. Nothing was sent.")
    if out.get("max_usd") is None:
        out["max_usd"] = implied_max(usd)
    return out, px, None


def usd_line(order: dict, px: float, symbol: str = "") -> str:
    """The quote's conversion line: the dollars, the token count and the price."""
    what = symbol or ("native" if order["token"] == _NATIVE else "tokens")
    return (f"  amount: ${order['usd']:,.2f} = {fmt_tokens(order['amount'])} {what} "
            f"at {fmt_price(px)} each (spend-grade price, read now; a send re-reads "
            f"it and is held to ${order['max_usd']:,.2f})\n")


def _checksum_ok(addr: str) -> bool:
    """EIP-55: an all-lower or all-upper hex address carries no checksum; a
    MIXED-case one must match it. A mistyped character in a copied address
    almost always breaks the case pattern — the one typo check an EVM address
    has. No keccak available = no check (never a false refusal)."""
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True
    try:
        from eth_utils import is_checksum_address
    except Exception:
        return True
    try:
        return bool(is_checksum_address(addr))
    except Exception:
        return True
