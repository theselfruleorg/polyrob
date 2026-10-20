"""The read-side merged screen also gates non-canonical EVM acquisitions."""
import asyncio

from tools.defi import token_screen
from tools.defi.providers import goplus


def screen_refusal(screen, *, owner_pinned: bool = False) -> str | None:
    """``owner_pinned``: the owner pinned this contract (``/wallet trust``), so
    a check no provider ANSWERED does not refuse it (owner decision 2026-10-16:
    GoPlus never answers is_honeypot for USDG on robinhood). A hard failure or
    a high sell tax still refuses."""
    if screen.hard_fails:
        return "refused: the buy screen found a hard failure: " + ", ".join(screen.hard_fails)
    answered = {check.name for check in screen.checks}
    missing = {"is_honeypot", "sell_tax"} - answered
    if missing and not owner_pinned:
        return "refused: the buy screen did not answer " + ", ".join(sorted(missing))
    if any(flag.startswith("sell_tax_") for flag in screen.flags):
        return "refused: the buy screen reports a high sell tax"
    return None


async def evm_buy_refusal(chain: str, token: str, identity, *,
                          owner_pinned: bool = False) -> str | None:
    if getattr(identity, "source", None) == "canonical":
        return None
    try:
        verdict, facts = await asyncio.gather(
            asyncio.to_thread(goplus.screen, chain, token),
            asyncio.to_thread(token_screen.gather_facts, chain, token))
        screen = token_screen.merge([token_screen.from_goplus(verdict, "evm"), *facts])
    except Exception:
        return "refused: the buy screen is unavailable; nothing was broadcast"
    return screen_refusal(screen, owner_pinned=owner_pinned)


def unchecked_route_refusal(verdict: str, max_spend_usd: float, *,
                            trusted: bool = False) -> str | None:
    """The $5 ticket for a route no independent price confirms.

    A TRUSTED ``token_out`` (owner pin, own launch, the run's owner target)
    whose route is UNAVAILABLE is not ticket-capped (owner decision
    2026-10-10): on a chain no price source covers, its route can never
    AGREE, and the cap refused every owner buyback. The per-tx ceiling, the
    slippage bound and the route's minimum output still bound it. A route
    that DISAGREES is never lifted, and an untrusted token keeps the ticket."""
    from tools.defi.identity_gate import UNVERIFIED_UNCHECKED_MAX_USD
    if trusted and verdict == "UNAVAILABLE":
        return None
    if verdict != "AGREES" and max_spend_usd > UNVERIFIED_UNCHECKED_MAX_USD:
        return ("refused: an independently unchecked route is limited to "
                f"${UNVERIFIED_UNCHECKED_MAX_USD:.2f} unless the token is trusted "
                "(owner pin, own launch or the owner's target) and the route does "
                "not disagree. Token identity does not establish a fair execution price. "
                "The owner pins a token with the chat verb "
                "`/wallet trust <chain> <address> <SYMBOL> go` — there is no "
                "setting that raises this limit.")
    return None


def is_sell_into_quote(token_out_is_quote: bool, token_in_is_quote: bool) -> bool:
    """085 EV-2: a swap of a non-quote token INTO the chain's canonical quote
    asset (USDC, WETH, wSOL ...) is a sell, and the asset at risk is
    ``token_in``. Anything else is a buy, and the asset at risk is
    ``token_out``."""
    return bool(token_out_is_quote and not token_in_is_quote)


def owner_confirms(tool, execution_context, params) -> bool:
    """True when the owner is asking in this turn, or approved this very call
    (``tx_guard.owner_authority``). Fails closed to False."""
    try:
        from core.wallet import tx_guard
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        direct, granted = tx_guard.owner_authority(
            execution_context, _is_forged_or_autonomous_turn, tool, params)
        return bool(direct or granted)
    except Exception:
        return False


def unpriced_sell_owner_queue(verdict: str, max_spend_usd: float, *,
                              trusted_in: bool, owner_ok: bool) -> str | None:
    """085 EV-2 / D5: the $5 ticket for a SELL whose route no independent
    price confirms, tested on the token SOLD (the asset at risk).

    The quote asset bought is always trusted, so before 085 every sell lifted
    the ticket. An untrusted token sold above the ticket on an unchecked route
    now goes to the OWNER (the owner confirms the exit) — never a plain
    refusal, so an exit stays possible. A trusted token sold (owner pin, own
    launch, owner target) keeps the 2026-10-10 lift. ``owner_ok``: the owner
    is asking in this turn or approved this call."""
    from tools.defi.identity_gate import UNVERIFIED_UNCHECKED_MAX_USD
    if trusted_in or owner_ok or verdict == "AGREES":
        return None
    if max_spend_usd <= UNVERIFIED_UNCHECKED_MAX_USD:
        return None
    return ("owner approval required: no independent price checks this sell's "
            f"route and the token sold is not trusted, so a sell above "
            f"${UNVERIFIED_UNCHECKED_MAX_USD:.2f} needs the owner to confirm it "
            "(a thin pool can be pushed down just before an exit). Propose the "
            "/swap to the owner with propose_action, or the owner runs it. "
            "lane: owner_queue. RESULT: NOT SENT — nothing was broadcast.")
