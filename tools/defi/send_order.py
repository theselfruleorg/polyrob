"""The ``/send`` order: the words of a send, parsed and proven (owner verb and
``propose_action`` read the SAME parser; it lives in the tools tier so the agent's
card proposal can quote a send before it makes a card). Moved verbatim from
``surfaces/telegram/send_ops.py``, which re-exports every name."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.surfaces.cards import GO_WORDS as _GO

_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_NATIVE = "native"
#: The owner's extra confirmation for a token whose value is NOT VERIFIED (no
#: spend-grade price): a wallet sends any token, after one more deliberate step.
ANYWAY = "anyway"

USAGE = ("Usage: /send <amount> <native|token-address> to <address> on <chain> "
         "[max <usd>] [anyway] [go]\n"
         "e.g. /send 0.5 native to 0x2FAa…f0e7 on ethereum      — quote only\n"
         "     /send 0.5 native to 0x2FAa…f0e7 on ethereum go   — send\n"
         "     /send 25 0x8335…2913 to 0xAbc…123 on base go     — a token\n"
         "     /send 1.2 sol to 7xKX…9fQ on solana go\n\n"
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


def parse(args: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    """``(order, None)`` or ``(None, error)``. Order keys: amount, token, to,
    chain, go. Word order is free apart from the ``to``/``on`` markers."""
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
    try:
        amount = float(rest[0])
    except ValueError:
        return None, f"Amount must be a positive number, got {rest[0]!r}.\n\n{USAGE}"
    if not (amount > 0):
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
    return {"amount": amount, "token": "native" if native else token, "to": to,
            "chain": chain, "go": go, "max_usd": max_usd, "anyway": anyway}, None


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
