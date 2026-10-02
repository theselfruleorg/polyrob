"""067 P5a: a destination's data routes are a contribution (webview/contributions.py)."""
import pytest
from fastapi import FastAPI

import webview.contributions as W

MONEY_PATHS = {"/api/webgate/wallet", "/api/webgate/ledger", "/api/webgate/positions",
               "/api/webgate/liquidity", "/api/webgate/book", "/api/webgate/invoices",
               "/api/webgate/invoices/{request_id}/settle", "/api/webgate/bridges",
               "/api/webgate/creations", "/api/webgate/moves",
               # W1: the tokens Rob trusts + the owner's word on one
               "/api/webgate/tokens", "/api/webgate/tokens/{action}"}


def _paths(routers):
    return {r.path for one in routers for r in one.routes}


def test_the_money_readers_are_the_money_contribution():
    import webview.pages as pages
    import webview.pages_new as pages_new
    assert _paths(W.contributed_routers("money")) == MONEY_PATHS
    assert W.provides("money")
    # the destination routers no longer carry them
    assert not MONEY_PATHS & _paths([pages.router, pages_new.api_router, pages_new.router])


def test_mount_serves_the_contribution():
    import webview.pages_new as pages_new
    from webview.pages_new import mount, served_method_paths
    app = FastAPI()
    mount(app)
    http, _ = served_method_paths(app)
    assert MONEY_PATHS <= {p for _m, p in http}
    del pages_new


def test_a_contribution_serves_one_of_the_five_destinations():
    from fastapi import APIRouter
    with pytest.raises(ValueError, match="unknown destination"):
        W.register_console_router(APIRouter(), destination="casino", source="t")
    with pytest.raises(ValueError, match="not a router"):
        W.register_console_router(object(), destination="money", source="t")


def test_without_a_money_contribution_the_page_is_the_install_hint(monkeypatch):
    from fastapi.testclient import TestClient

    import webview.pages_new as pages_new
    from webview.copy import t
    W.contributed_routers()
    monkeypatch.setattr(W, "_ROUTERS", [])
    monkeypatch.setattr(W, "_IN_TREE", ())
    assert not W.provides("money")
    app = FastAPI()
    app.include_router(pages_new.router)
    monkeypatch.setattr(pages_new, "_pause_headline", lambda: "")
    monkeypatch.setattr(pages_new, "_inbox_state", lambda request: (0, False, 0))
    monkeypatch.setattr(pages_new, "_read_only", lambda: False)
    html = TestClient(app).get("/money").text
    assert t("money.not_installed") in html
    assert "money.js" not in html and 'data-tab="book"' not in html
