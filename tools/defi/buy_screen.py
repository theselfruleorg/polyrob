"""The read-side merged screen also gates non-canonical EVM acquisitions."""
import asyncio

from tools.defi import token_screen
from tools.defi.providers import goplus


def screen_refusal(screen) -> str | None:
    if screen.hard_fails:
        return "refused: the buy screen found a hard failure: " + ", ".join(screen.hard_fails)
    answered = {check.name for check in screen.checks}
    missing = {"is_honeypot", "sell_tax"} - answered
    if missing:
        return "refused: the buy screen did not answer " + ", ".join(sorted(missing))
    if any(flag.startswith("sell_tax_") for flag in screen.flags):
        return "refused: the buy screen reports a high sell tax"
    return None


async def evm_buy_refusal(chain: str, token: str, identity) -> str | None:
    if getattr(identity, "source", None) == "canonical":
        return None
    try:
        verdict, facts = await asyncio.gather(
            asyncio.to_thread(goplus.screen, chain, token),
            asyncio.to_thread(token_screen.gather_facts, chain, token))
        screen = token_screen.merge([token_screen.from_goplus(verdict, "evm"), *facts])
    except Exception:
        return "refused: the buy screen is unavailable; nothing was broadcast"
    return screen_refusal(screen)


def unchecked_route_refusal(verdict: str, max_spend_usd: float) -> str | None:
    from tools.defi.identity_gate import UNVERIFIED_UNCHECKED_MAX_USD
    if verdict != "AGREES" and max_spend_usd > UNVERIFIED_UNCHECKED_MAX_USD:
        return ("refused: an independently unchecked route is limited to "
                f"${UNVERIFIED_UNCHECKED_MAX_USD:.2f}, including trusted tokens. "
                "Token identity does not establish a fair execution price.")
    return None
