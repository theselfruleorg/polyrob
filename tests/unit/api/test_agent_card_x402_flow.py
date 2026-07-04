"""F4 (N5): the Agent Card must advertise the LIVE x402 flow, not the dead one.

The card told clients to "POST /api/x402/create-payment" — an endpoint that only
ever 503s (its x402_handler is never set). The real, working path is the standard
x402 header flow: request -> 402 challenge -> retry with an X-PAYMENT header
(handled by X402PaymentMiddleware).
"""
from api.a2a.agent_card import build_agent_card


def test_card_advertises_xpayment_header_flow():
    card = build_agent_card()
    how = card.pricing["authentication_options"]["x402"]["how_to_use"]
    assert "X-PAYMENT" in how
    # The dead create-payment flow must no longer be advertised.
    assert "create-payment" not in how


def test_card_x402_price_matches_live_charge():
    from modules.x402.x402_integration import get_x402_price_usd
    card = build_agent_card()
    # The card must state exactly the live charge (now economics-derived, not $0.01).
    assert card.pricing["authentication_options"]["x402"]["per_request_usd"] == get_x402_price_usd()


def test_x402_security_scheme_header_matches_middleware():
    # modules/x402/middleware.py reads request.headers.get("X-PAYMENT") — the
    # advertised SecurityScheme.name must match, or an external client following
    # the card literally sends the wrong header and gets silently ignored.
    card = build_agent_card()
    assert card.securitySchemes["x402"].name == "X-PAYMENT"
