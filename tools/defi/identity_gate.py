"""Which contract a BUY may name (068 G1).

On 2026-09-25 a buyback cron bought an airdropped look-alike: the text said
"PNL", the reconcile output listed the fake "PNL" first, the model took that
address, and every check that ran said so — ``token_info`` returned
``verified: false``, the dry run said ``route check: UNAVAILABLE`` — and the
swap verb read none of it. A skill can TEACH "resolve by address"; only the verb
can make a wrong address impossible to spend on. That is this module.

Three refusals, checked before any quote is built. Each is about the token BEING
BOUGHT (``token_out``); a sell into a canonical or pinned asset is never touched:

1. **Pinned symbol, other contract.** The chain pins this symbol (canonical list
   or an owner pin) to a different address. Hard refusal.
2. **Tracked position, other contract.** The book holds an OPEN position whose
   symbol is this one, at a different address, and ``token_out`` is not
   trusted. Two contracts, one name — the owner says which one they mean.
3. **Unverified and unchecked, above a small ticket.** ``token_out`` is not
   trusted AND the independent route check did not AGREE AND the declared
   ceiling is above ``UNVERIFIED_UNCHECKED_MAX_USD``. The degen ladder's largest
   ticket is $5, so memecoin scouting is untouched; a $134 buy of an unknown
   contract nobody can price is not.

W0 — trust has four sources, none the agent can grant itself: ``canonical``,
``owner_pin``, ``own_launch`` (this instance launched/deployed it —
``core.wallet.token_provenance``, probed on-chain here on a miss) and the run's
OWNER-authored ``target_token`` (``buy_target.owner_target_matches``). When a
TRUSTED ``token_out`` collides with an UNTRUSTED tracked claimant, the claimant
is QUARANTINED (``core.open_positions``: it keeps its cost basis, it is no
longer a symbol claim) and the buy proceeds — a look-alike can no longer block
the real token forever. If neither side is trusted, rule 2 still refuses.

No refusal here tells anyone to run a shell command. W1: when no candidate is
trusted (rules 2 and 3), the gate itself raises ONE owner ask of kind
``token_identity`` in ``/pending`` (``tools/defi/token_identity_ask.py``) — the
owner taps trust / not trusted — and the refusal tells the agent it was asked,
so the agent never messages the owner about it separately.

``execution_context is None`` is the owner-direct CLI call and is not gated
(parity with ``_non_route_grant_refusal``). Every read here fails OPEN to "no
evidence" for rules 1–2 (an unreadable pin table cannot invent a collision) —
rule 3 still applies, because an unreadable pin leaves the token unverified.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: The largest ticket (USD) an autonomous buy of an unverified token may carry
#: when no independent price agrees with the route. The treasury skill's Tier A
#: ticket; owner pins and an AGREES route check both lift it.
UNVERIFIED_UNCHECKED_MAX_USD = 5.0

_PIN_REMEDY = ("A contract counts as the real one only when it is trusted: this "
               "instance launched or deployed it, the owner wrote it into this "
               "job as its target_token, or the owner pinned it. Do not guess and "
               "do not retry with another address. Ask the owner in chat with "
               "your ask tool, naming both contract addresses, which one they "
               "mean — never tell anyone to run a shell command. Nothing was "
               "broadcast.")


def _norm_symbol(symbol: Optional[str]) -> str:
    return str(symbol or "").strip().upper()


class _Unreadable(Exception):
    pass


def _tracked_claims(user_id: str, chain: str, symbol: str, token_out: str) -> list:
    """Tracked positions on *chain* claiming *symbol* at another address.

    068 B1: strict — a store that exists but cannot be read raises
    ``_Unreadable``; the caller refuses rather than reading it as no claims.
    """
    from core.wallet.addresses import same_address
    try:
        from core import open_positions
        entries = open_positions.entries_for(user_id, strict=True)
    except Exception as exc:
        logger.warning("identity gate: open positions unreadable", exc_info=True)
        raise _Unreadable(f"the tracked-position store ({type(exc).__name__})") from exc
    out = []
    for _addr, entry in entries.items():
        if str(getattr(entry, "status", "open") or "open") != "open":
            continue  # W0: a quarantined / written-off row is no longer a claim
        if (str(entry.chain or "").strip().lower() == chain
                and _norm_symbol(entry.symbol) == symbol
                and not same_address(entry.address, token_out)):
            out.append(entry)
    return out


def _own_launch(chain: str, token: str, *, probe: bool) -> bool:
    try:
        from tools.launchpad import provenance as _probes  # noqa: F401 — registers
    except Exception:
        logger.debug("identity gate: launch probes unavailable", exc_info=True)
    try:
        from core.wallet.token_provenance import own_token
        return own_token(chain, token, probe=probe) is not None
    except Exception:
        logger.debug("identity gate: provenance read failed", exc_info=True)
        return False


def _trust_source(id_out, *, chain: str, token_out: str, execution_context) -> str:
    """Why ``token_out`` is trusted for this buy, or ``""`` when it is not."""
    if getattr(id_out, "verified", False):
        return str(getattr(id_out, "source", None) or "verified")
    try:
        # W1: the owner's binding (CLI pin or an approved identity ask) is read
        # here directly, so a caller's stale identity object cannot miss it.
        from core.wallet.token_pins import owner_pin
        pinned = owner_pin(chain, token_out)
        if pinned is not None:
            return str(pinned.get("source") or "owner_pin")
    except Exception:
        logger.debug("identity gate: pin read failed", exc_info=True)
    try:
        from core.wallet.buy_target import owner_target_matches
        if owner_target_matches(execution_context, chain=chain, token=token_out):
            return "owner_target"
    except Exception:
        logger.debug("identity gate: target read failed", exc_info=True)
    if _own_launch(chain, token_out, probe=True):
        return "own_launch"
    return ""


def _claimant_trusted(chain: str, address: str) -> bool:
    """A tracked claimant that is itself trusted is never quarantined."""
    try:
        from core.wallet.tokens import canonical_token
        if canonical_token(chain, address) is not None:
            return True
    except Exception:
        pass
    try:
        from core.wallet.token_pins import owner_pin
        if owner_pin(chain, address, strict=True) is not None:
            return True
    except Exception:
        return True  # cannot prove it untrusted: leave the row alone
    return _own_launch(chain, address, probe=False)


def _quarantine_lookalikes(user_id: str, chain: str, symbol: str, token_out: str,
                           source: str) -> None:
    """W0: a trusted buy quarantines the UNTRUSTED tracked claimants of its
    symbol. Best-effort: an unreadable book cannot block a trusted buy."""
    if not user_id or not symbol:
        return
    try:
        claims = _tracked_claims(user_id, chain, symbol, token_out)
    except _Unreadable:
        return
    for entry in claims:
        if _claimant_trusted(chain, entry.address):
            continue
        reason = (f"look-alike: claims {symbol} on {chain}, but the trusted "
                  f"{symbol} is {token_out} ({source})")
        try:
            from core import open_positions
            changed = open_positions.set_status(
                user_id, entry.chain, entry.address, open_positions.STATUS_QUARANTINED,
                reason=reason, account=getattr(entry, "account", "") or "")
        except Exception:
            logger.warning("identity gate: quarantine write failed", exc_info=True)
            continue
        if changed:
            logger.warning("identity gate: QUARANTINED tracked %s %s on %s — %s",
                           symbol, entry.address, chain, reason)
            _emit_quarantine(user_id, chain, symbol, entry.address, token_out, source)


def _emit_quarantine(user_id, chain, symbol, address, trusted, source) -> None:
    try:
        from core.event_kinds import TOKEN_QUARANTINED
        from core.event_log import emit
        emit(TOKEN_QUARANTINED, source="identity_gate", user_id=user_id,
             attrs={"chain": chain, "symbol": symbol, "address": address,
                    "trusted": trusted, "trust_source": source})
    except Exception:
        logger.debug("identity gate: quarantine event not recorded", exc_info=True)


def _unreadable_refusal(what: str, shown: str) -> str:
    return (f"refused: {what} exists but could not be read, so nothing can "
            f"confirm that {shown} is the contract the owner means — an "
            f"unreadable store is not an empty one. Buys of unverified tokens "
            f"stay refused until it reads again. Tell the owner in chat that "
            f"this store cannot be read (its file permissions on the server) — "
            f"never tell anyone to run a shell command. Nothing was broadcast.")


def buy_identity_refusal(*, chain: str, token_out: str, id_out,
                         max_spend_usd: float, route_verdict: str,
                         execution_context, container=None) -> Optional[str]:
    """None when this buy may proceed, else the refusal text.

    W1: a refusal with no trusted candidate (rules 2 and 3) raises the owner's
    token-identity ask (``tools/defi/token_identity_ask.py``) — ``container``
    lets its one notice reach the owner's chat."""
    if execution_context is None:
        return None
    from core.wallet.addresses import same_address
    from core.wallet.token_pins import PinStoreUnreadable, norm_chain
    chain = norm_chain(chain)
    symbol = _norm_symbol(getattr(id_out, "symbol", None))
    name = getattr(id_out, "name", None) or "name unknown"
    shown = f"{symbol or '?'} {token_out} ({name})"
    canonical = getattr(id_out, "source", None) == "canonical"

    if symbol:
        try:
            from core.wallet.token_pins import pinned_addresses_for_symbol
            pinned = [a for a in pinned_addresses_for_symbol(chain, symbol, strict=True)
                      if not same_address(a, token_out)]
        except PinStoreUnreadable:
            if not canonical:
                return _unreadable_refusal("the owner token-pin store", shown)
            pinned = []
        if pinned:
            return (f"refused: {symbol} on {chain} is PINNED to {pinned[0]}, and "
                    f"you asked to buy a different contract: {shown}. A symbol "
                    f"is a claim any contract can make; the pin is the owner's "
                    f"word for which one is real. Use the pinned address. "
                    + _PIN_REMEDY)
    elif not getattr(id_out, "verified", False):
        # No symbol to collide on: still prove the pin store is readable before
        # an unverified buy, or an unreadable store silently lifts nothing.
        try:
            from core.wallet.token_pins import all_pins
            all_pins(chain=chain, strict=True)
        except PinStoreUnreadable:
            return _unreadable_refusal("the owner token-pin store", shown)

    if not canonical:
        # W1: the owner said this contract is NOT the token. That word binds
        # the buy; nothing is asked again.
        try:
            from core.wallet.token_pins import rejection
            rejected = rejection(chain, token_out, strict=True)
        except PinStoreUnreadable:
            return _unreadable_refusal("the owner token-pin store", shown)
        if rejected:
            return (f"refused: the owner marked {shown} NOT trusted"
                    + (f" ({rejected.get('note')})" if rejected.get("note") else "")
                    + ". Do not buy it, do not ask about it again, and do not retry "
                      "with another address. Nothing was broadcast.")

    uid = str(getattr(execution_context, "user_id", "") or "")
    source = _trust_source(id_out, chain=chain, token_out=token_out,
                           execution_context=execution_context)
    if source:
        _quarantine_lookalikes(uid, chain, symbol, token_out, source)
        return None

    if symbol:
        try:
            tracked = _tracked_claims(uid, chain, symbol, token_out) if uid else []
        except _Unreadable as exc:
            return _unreadable_refusal(str(exc), shown)
        if tracked:
            text = (f"refused: you hold a tracked {symbol} position at "
                    f"{tracked[0].address} on {chain}, and this buy names a "
                    f"DIFFERENT contract that also calls itself {symbol}: "
                    f"{shown}, and that contract is not trusted. One symbol, two "
                    f"contracts — do not pick one by symbol, by liquidity, or "
                    f"by which line came first. ")
            cands = [(token_out, getattr(id_out, "name", None), "the buy you asked for")]
            cands += [(t.address, None, "a tracked position") for t in tracked]
            return text + _ask_remedy(uid, chain, symbol, cands, text, container)

    if route_verdict != "AGREES" and max_spend_usd > UNVERIFIED_UNCHECKED_MAX_USD:
        text = (f"refused: {shown} is UNVERIFIED (not canonical, not our own "
                f"launch, not the owner's target or pin) and the independent route check did not agree "
                f"({route_verdict}), so nothing confirms this is the token you "
                f"mean or that the price is real. An unverified, unchecked buy "
                f"is limited to ${UNVERIFIED_UNCHECKED_MAX_USD:.2f} "
                f"(max_spend_usd={max_spend_usd:.2f} was declared). ")
        cands = [(token_out, getattr(id_out, "name", None), "the buy you asked for")]
        return (text + f"A ticket of ${UNVERIFIED_UNCHECKED_MAX_USD:.2f} or less "
                f"passes now. Otherwise: "
                + _ask_remedy(uid, chain, symbol, cands, text, container))
    return None


def container_of(tool) -> object:
    """The tool's DI container, or None — a tool built outside a container
    raises from its ``container`` property, and a refusal must still stand."""
    try:
        return getattr(tool, "container", None)
    except Exception:
        return None


def _ask_remedy(uid: str, chain: str, symbol: str, cands: list, reason: str,
                container) -> str:
    """W1: raise (or refresh) the owner's token-identity ask and say so. Falls
    back to the chat remedy only when the ask could not be written."""
    raised = None
    if uid:
        try:
            from tools.defi.token_identity_ask import (describe_candidate,
                                                       raise_identity_ask)
            candidates = [describe_candidate(uid, chain, addr, symbol=symbol,
                                             name=name or "", role=role)
                          for addr, name, role in cands]
            raised = raise_identity_ask(user_id=uid, chain=chain, symbol=symbol,
                                        candidates=candidates, reason=reason,
                                        container=container)
        except Exception:
            logger.warning("identity gate: token identity ask failed", exc_info=True)
            raised = None
    if not raised:
        return _PIN_REMEDY
    ask_id, created = raised
    return (f"I {'asked' if created else 'already asked'} the owner in /pending "
            f"(ask {ask_id[:12]}) which contract is real; the owner decides with a "
            f"tap, and a trusted contract passes on the next run. Do NOT message "
            f"the owner about this, do NOT call owner_ask, and do not retry with "
            f"another address — skip this buy and continue. Never tell anyone to "
            f"run a shell command. Nothing was broadcast.")
