"""`/pay` — the owner pays for one x402 resource from chat (068 X4/D1).

Until 2026-09-26 no seat could pay an x402 challenge: `x402_pay` was in no prod
session's toolset, `load_tool` answered "gated: money", and there was no owner
verb. The owner could not pay $0.05 for one API call from anywhere.

⚠️ REACH, not policy. This calls the SAME ``X402PayTool.x402_fetch`` the agent
calls, with a genuine owner context, so every gate under it still applies: the
URL/SSRF validator, the network and canonical-USDC asset pins, the
``max_amount_usd`` cap (probe, SDK spend control, amount policy), the PolicyGate
per-transaction ceiling and ``WALLET_VENUE_DAILY_CAP_X402_USD``, the 600 s
authorization window, the submission journal and the owner pause. Typing ``go``
is the owner's own act; there is no second tap.

Bare form QUOTES (never pays, needs no wallet). Shared by the REPL.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

#: What reaches the chat of a paid response body. The full body is the agent's
#: business; the owner needs to see that it arrived and what it starts with.
_BODY_PREVIEW_CHARS = 1500

USAGE = (
    "Usage: /pay <url> [max_usd] [go] [id=<name>]\n"
    "e.g. /pay https://api.example.com/v1/data            — price only, pays nothing\n"
    "     /pay https://api.example.com/v1/data 0.10 go    — pay up to $0.10 and fetch\n\n"
    "GET only. It pays in USDC on Base, and only if the price is at or below "
    "max_usd. The wallet caps still apply. Each /pay message is its own payment; "
    "a re-delivered message never pays twice (id=<name> names it yourself)."
)


def _positive(raw: str) -> Optional[float]:
    try:
        value = float(str(raw).lstrip("$"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def pay_reply(user_id: Optional[str], args: List[str], *,
                    request_id: Optional[str] = None) -> str:
    """``/pay <url> [max_usd] [go] [id=<name>]``. One chat-ready string; never raises.

    068 B9: ``request_id`` names THIS command (the seat passes the inbound
    message identity — Telegram's ``update_id`` — so a re-delivered message maps
    to the same x402 replay key and can never pay twice, while a new ``/pay`` of
    the same URL tomorrow is a new payment). ``id=<name>`` in the args overrides
    it. Without either, the old one-payment-per-(url, cap) key applies.
    """
    if not user_id:
        return "Only the owner can pay."
    tokens = [str(a).strip() for a in (args or []) if str(a).strip()]
    explicit = [t for t in tokens if t.lower().startswith("id=")]
    tokens = [t for t in tokens if not t.lower().startswith("id=")]
    if explicit:
        request_id = explicit[-1][3:].strip() or request_id
    request_id = (str(request_id).strip()[:128] or None) if request_id else None
    if not tokens:
        return USAGE
    execute = False
    if tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()
    if not tokens:
        return USAGE
    url = tokens[0]
    if not url.lower().startswith(("https://", "http://")):
        return f"That is not a URL: {url!r}\n\n{USAGE}"
    max_usd = None
    if len(tokens) > 1:
        max_usd = _positive(tokens[1])
        if max_usd is None:
            return f"max_usd must be a positive number, got {tokens[1]!r}."

    try:
        from tools.x402.service import FetchParams, QuoteParams, X402PayTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The x402 payment rail is unavailable: {exc}"
    from surfaces.telegram.token_ops import _owner_ctx

    tool = X402PayTool()
    if not execute:
        try:
            result = await tool.x402_quote(QuoteParams(url=url), _owner_ctx(user_id))
        except Exception as exc:
            logger.warning("x402 quote failed", exc_info=True)
            return f"The quote did not run: {exc}"
        if getattr(result, "error", None):
            return f"❌ {result.error}"
        hint = (f"\nTo pay: /pay {url} {max_usd:g} go" if max_usd
                else f"\nTo pay: /pay {url} <max_usd> go")
        return f"{getattr(result, 'extracted_content', '') or ''}{hint}"

    if max_usd is None:
        return ("Say the most you will pay: /pay <url> <max_usd> go — "
                "I never pay an amount you did not name.")
    try:
        result = await tool.x402_fetch(
            FetchParams(url=url, max_amount_usd=max_usd, request_id=request_id),
            _owner_ctx(user_id))
    except Exception as exc:
        logger.warning("x402 fetch failed", exc_info=True)
        return f"The payment did not run: {exc}"
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = str(getattr(result, "extracted_content", "") or "")
    if len(body) > _BODY_PREVIEW_CHARS:
        body = body[:_BODY_PREVIEW_CHARS] + f"\n… ({len(body) - _BODY_PREVIEW_CHARS} more characters)"
    return body or "Done — the response was empty."
