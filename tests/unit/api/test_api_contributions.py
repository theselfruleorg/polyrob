"""067 P5a: the money rail's API pieces are contributions (api/contributions.py)."""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.contributions as C


def _req(path="/a2a/rpc", **state):
    r = Request({"type": "http", "path": path, "headers": [], "query_string": b"",
                 "method": "POST", "server": ("x", 80), "scheme": "http", "root_path": ""})
    for k, v in state.items():
        setattr(r.state, k, v)
    return r


def test_the_in_tree_money_contributions_fill_every_slot():
    assert {c.slot for c in C.router_contributions()} == set(C.ROUTER_SLOTS)
    assert [n for n, _ in C.auth_methods()] == ["x402"]
    assert [n for n, _ in C.payment_options()] == ["x402"]
    assert [n for n, _ in C.card_payment_options()] == ["x402"]
    assert "payment" in C._MIDDLEWARE


def test_a_slot_core_did_not_name_is_refused():
    with pytest.raises(ValueError, match="not a slot"):
        C.register_router("mine", "m:r", source="t")
    with pytest.raises(ValueError, match="not a slot"):
        C.register_middleware("mine", "m:f", source="t")
    with pytest.raises(ValueError, match="module:attr"):
        C.register_auth_method("x", "nocolon", source="t")


@pytest.fixture
def no_rail(monkeypatch):
    C.router_contributions()                       # load the in-tree rail first
    monkeypatch.setattr(C, "_ROUTERS", [])
    monkeypatch.setattr(C, "_MIDDLEWARE", {})
    monkeypatch.setattr(C, "_AUTH", [])
    monkeypatch.setattr(C, "_PAYMENT_OPTIONS", [])
    monkeypatch.setattr(C, "_CARD_OPTIONS", [])


def test_without_the_rail_the_app_has_no_money_routes_or_middleware(no_rail, monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    from api.app import create_app
    app = create_app()
    paths = set()

    def walk(routes, pre=""):
        for r in routes or ():
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes, pre + (getattr(r, "prefix", "") or ""))
                continue
            paths.add(pre + (getattr(r, "path", "") or ""))
    walk(app.routes)
    assert not [p for p in paths if p.startswith(("/api/x402", "/api/payments", "/eip8004"))]
    assert "X402PaymentMiddleware" not in [m.cls.__name__ for m in app.user_middleware]


def test_without_the_rail_x402_state_is_not_an_identity(no_rail):
    from api.dependencies import get_user_permissive
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_user_permissive(_req(payment_method="x402")))
    assert e.value.status_code == 401


def test_without_the_rail_a_402_offers_credits_only(no_rail):
    from api.payment_verification import payment_required_response
    body = payment_required_response(_req(), 2)
    assert set(body["payment_options"]) == {"credits"}


def test_without_the_rail_the_card_omits_x402(no_rail):
    from api.a2a.agent_card import build_agent_card
    card = build_agent_card()
    assert "x402" not in card.securitySchemes
    assert "x402" not in card.pricing["authentication_options"]


def test_the_x402_auth_path_is_unchanged():
    from api.dependencies import get_user_permissive
    assert asyncio.run(get_user_permissive(_req(payment_method="x402", user_id="u1"))) == "u1"
    assert asyncio.run(get_user_permissive(_req(payment_method="x402"))) == "x402_user"
    assert asyncio.run(get_user_permissive(_req(user_id="jwt"))) == "jwt"


def test_api_core_files_name_no_x402_module():
    """The P5c worklist shrinks: these call sites reach the rail only through
    the seam."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("api/dependencies.py", "api/payment_verification.py", "api/a2a/agent_card.py"):
        text = (root / rel).read_text()
        assert "modules.x402" not in text and "x402_advertisement import treasury" not in text, rel
