"""043 C5 — the Inbox endpoint and the Inbox page.

The endpoint is thin on purpose: the composition lives in
``core.surfaces.inbox`` (pure, tested in ``tests/unit/core/surfaces/test_inbox.py``)
and the readers in ``surfaces.inbox_sources``. What is tested HERE is the part
only a route can get wrong — the seams a test can replace, the tenant, the
mutation guard, and the three states the page must render without ever saying
"nothing needs you" over a list it could not read.
"""
import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.surfaces.inbox import Item


@pytest.fixture()
def inbox(monkeypatch):
    import webview.inbox as mod
    monkeypatch.setattr(mod, "_tenant", lambda request: "u1")
    return mod


@pytest.fixture()
def client(inbox):
    app = FastAPI()
    app.include_router(inbox.router)
    return TestClient(app)


def _all_empty(monkeypatch, mod):
    for name in ("_collect_self_evolution", "_collect_tool_approvals",
                 "_collect_correspondents", "_collect_asks", "_collect_apps"):
        monkeypatch.setattr(mod, name, lambda uid: [])


# --- the composition, through the module's own seams ------------------------ #

def test_inbox_names_an_unreadable_source_and_never_says_nothing(monkeypatch):
    import webview.inbox as inbox
    monkeypatch.setattr(inbox, "_collect_self_evolution", lambda uid: [])
    monkeypatch.setattr(inbox, "_collect_tool_approvals",
                        lambda uid: (_ for _ in ()).throw(RuntimeError("goals.db locked")))
    monkeypatch.setattr(inbox, "_collect_correspondents", lambda uid: [])
    monkeypatch.setattr(inbox, "_collect_asks", lambda uid: [])
    monkeypatch.setattr(inbox, "_collect_apps", lambda uid: [])
    body = inbox.build_inbox("u1")
    assert body["uncertain"] is True and body["count"] == 0
    assert body["sources"]["tool_approvals"].startswith("unreadable(")


def test_badge_counts_decisions_not_information():
    import webview.inbox as inbox
    items = [inbox.Item(kind="ask", id="1", blocking=True, expires_at=None,
                        created_at=1.0, title="x"),
             inbox.Item(kind="invoice", id="2", blocking=False, expires_at=None,
                        created_at=0.5, title="late")]
    out = inbox.compose(items, sources={})
    assert out["count"] == 1 and [i["id"] for i in out["not_blocking"]] == ["2"]


def test_every_source_is_reported_even_when_all_are_clean(monkeypatch):
    import webview.inbox as inbox
    _all_empty(monkeypatch, inbox)
    body = inbox.build_inbox("u1")
    assert set(body["sources"]) == {"self_evolution", "tool_approvals",
                                    "correspondents", "asks", "apps"}
    assert set(body["sources"].values()) == {"ok"}
    assert body["uncertain"] is False


# --- the endpoint ----------------------------------------------------------- #

def test_the_endpoint_answers_the_composed_body(client, inbox, monkeypatch):
    _all_empty(monkeypatch, inbox)
    monkeypatch.setattr(inbox, "_collect_asks",
                        lambda uid: [Item(kind="ask", id="a1", title="need a key")])
    resp = client.get("/api/webgate/inbox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == "a1"
    assert body["user_id"] == "u1"


def test_the_endpoint_never_500s_when_a_store_refuses(client, inbox, monkeypatch):
    _all_empty(monkeypatch, inbox)
    monkeypatch.setattr(inbox, "_collect_apps",
                        lambda uid: (_ for _ in ()).throw(OSError("disk")))
    resp = client.get("/api/webgate/inbox")
    assert resp.status_code == 200
    assert resp.json()["sources"]["apps"].startswith("unreadable(")


# --- the deciders are REUSED, never reimplemented ---------------------------- #

def test_decide_routes_a_self_evolution_item_to_the_shared_decider(
        client, inbox, monkeypatch):
    seen = {}

    def fake(kind, item_id, kw, *, approved):
        seen.update(kind=kind, item_id=item_id, approved=approved,
                    user_id=kw["user_id"])
        return True, "done"

    monkeypatch.setattr(inbox, "_decide_pending", fake)
    resp = client.post("/api/webgate/inbox/skill/abc/decide")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert seen == {"kind": "skill", "item_id": "abc", "approved": True,
                    "user_id": "u1"}


def test_reject_routes_with_approved_false(client, inbox, monkeypatch):
    seen = {}
    monkeypatch.setattr(inbox, "_decide_pending",
                        lambda k, i, kw, *, approved: (seen.update(approved=approved)
                                                       or (True, "ok")))
    client.post("/api/webgate/inbox/skill/abc/reject")
    assert seen == {"approved": False}


def test_an_ask_is_decided_on_the_goal_board(client, inbox, monkeypatch):
    calls = []

    class FakeBoard:
        def decide_ask(self, ask_id, *, user_id, approved):
            calls.append((ask_id, user_id, approved))
            return (True, 2)

    monkeypatch.setattr(inbox, "_goal_board", lambda: FakeBoard())
    resp = client.post("/api/webgate/inbox/ask/g7/fulfill")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert calls == [("g7", "u1", True)]


@pytest.mark.parametrize("unblocked,expected", [
    (0, "Done."), (1, "One goal can run again."), (3, "3 goals can run again."),
])
def test_an_asks_decision_says_what_it_actually_freed(client, inbox, monkeypatch,
                                                      unblocked, expected):
    """"1 goals" is the tell that a count was pasted into a sentence rather than
    written into one."""
    class FakeBoard:
        def decide_ask(self, ask_id, *, user_id, approved):
            return (True, unblocked)

    monkeypatch.setattr(inbox, "_goal_board", lambda: FakeBoard())
    resp = client.post("/api/webgate/inbox/ask/g7/fulfill")
    assert expected in resp.json()["message"]


def test_an_app_is_decided_through_owner_ops(client, inbox, monkeypatch):
    calls = []
    monkeypatch.setattr(inbox, "_decide_app",
                        lambda slug, uid, approved: (calls.append((slug, uid, approved))
                                                     or (True, "approved")))
    resp = client.post("/api/webgate/inbox/app/price-watch/decide")
    assert resp.status_code == 200
    assert calls == [("price-watch", "u1", True)]


def test_fulfill_is_refused_for_a_kind_that_cannot_be_fulfilled(client, inbox):
    resp = client.post("/api/webgate/inbox/skill/abc/fulfill")
    assert resp.status_code == 400


def test_an_unknown_verb_is_not_a_route(client):
    assert client.post("/api/webgate/inbox/skill/abc/delete").status_code == 404


def test_a_failed_decision_is_reported_not_raised(client, inbox, monkeypatch):
    monkeypatch.setattr(inbox, "_decide_pending",
                        lambda k, i, kw, *, approved: (False, "no such item"))
    resp = client.post("/api/webgate/inbox/skill/abc/decide")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "message": "no such item"}


# --- read-only: every mutation carries the console's one guard --------------- #

def test_every_mutation_carries_the_shared_guard(inbox):
    from webview import webgate
    mutating = [r for r in inbox.router.routes
                if "POST" in (getattr(r, "methods", None) or set())]
    assert mutating, "the Inbox has no mutations — this test would be vacuous"
    for route in mutating:
        names = {getattr(d.dependency, "__name__", "") for d in route.dependencies}
        assert "read_only_guard" in names, f"{route.path} is not read-only guarded"
        assert "csrf_guard" in names, f"{route.path} has no csrf guard"
    assert webgate.MUTATION_DEPS, "the shared guard list is empty"


def test_a_read_is_not_guarded(inbox):
    reads = [r for r in inbox.router.routes
             if "GET" in (getattr(r, "methods", None) or set())]
    assert reads
    for route in reads:
        assert not route.dependencies, f"{route.path} guards a read"


# --- the page: three states ------------------------------------------------- #

@pytest.fixture()
def page_client(monkeypatch):
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app), mod


def _summary(monkeypatch, mod, items, sources):
    from core.surfaces.inbox import compose
    body = compose(items, sources)
    monkeypatch.setattr(mod, "_inbox_summary", lambda request: body)
    return body


def _soup(client):
    resp = client.get("/inbox")
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def _page_text(soup) -> str:
    """The SCREEN's own words, without the frame's.

    The head truth legitimately says "Nothing needs you here, and one list is
    unreadable" — a qualified claim, which is the point. What the page itself
    may never do is state the unqualified version, so the assertions below read
    the page and not the whole document.
    """
    page = soup.select_one(".page")
    assert page is not None, "the Inbox rendered no page"
    return page.get_text(" ", strip=True)


def test_the_empty_state_names_what_it_checked(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [], {"asks": "ok", "apps": "ok"})
    text = _page_text(_soup(client))
    assert "Nothing needs you" in text
    assert "every source answered" in text


def test_the_item_state_lists_the_decisions(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod,
             [Item(kind="app", id="price-watch", title="price-watch",
                   body="put it online", actions=("approve", "reject"))],
             {"apps": "ok"})
    soup = _soup(client)
    assert "price-watch" in soup.get_text(" ", strip=True)
    assert soup.select(".entry")


def test_an_informational_card_is_listed_and_not_counted(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod,
             [Item(kind="invoice", id="1180", title="acme.dev is late",
                   blocking=False)],
             {"apps": "ok"})
    soup = _soup(client)
    text = soup.get_text(" ", strip=True)
    assert "Not blocking" in text and "acme.dev is late" in text
    # The frame's badge counts decisions; an informational card is not one.
    assert not soup.select(".nav-badge")


def test_the_partial_state_says_the_list_is_incomplete(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [], {"tool_approvals": "unreadable(OSError: disk)"})
    text = _page_text(_soup(client))
    assert "incomplete" in text
    assert "Nothing needs you" not in text


def test_the_partial_state_never_claims_nothing_waits(page_client, monkeypatch):
    """The whole point. A refused source and a zero must never look alike."""
    client, mod = page_client
    _summary(monkeypatch, mod, [], {"asks": "unreadable(locked)"})
    assert "Nothing needs you" not in _page_text(_soup(client))
    # …and the frame only ever says it with the qualifier attached.
    head = _soup(client).select_one(".head-truth").get_text(" ", strip=True)
    assert "one list is unreadable" in head


def test_the_page_carries_one_sources_note(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [], {"asks": "ok"})
    assert len(_soup(client).select("p.sources")) == 1


def test_the_page_has_one_main_nav(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [], {"asks": "ok"})
    assert len(_soup(client).select('nav[aria-label="Main"]')) == 1


# --- the frame's badge reads the SAME summary -------------------------------- #

def test_the_badge_reads_the_composed_count(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod,
             [Item(kind="ask", id="a", title="one"),
              Item(kind="ask", id="b", title="two")], {"asks": "ok"})
    badge = _soup(client).select_one(".nav-badge")
    assert badge is not None and badge.get_text(strip=True) == "2"


def test_an_uncertain_count_is_drawn_uncertain(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [Item(kind="ask", id="a", title="one")],
             {"asks": "ok", "apps": "unreadable(locked)"})
    soup = _soup(client)
    badge = soup.select_one(".nav-badge")
    assert badge is not None and badge.get_text(strip=True) == "1+"
    assert "is-uncertain" in badge.get("class", [])
    assert "one list" in soup.select_one(".nav-item[aria-label]")["aria-label"]


def test_two_unreadable_lists_are_counted_as_two(page_client, monkeypatch):
    client, mod = page_client
    _summary(monkeypatch, mod, [Item(kind="ask", id="a", title="one")],
             {"asks": "unreadable(locked)", "apps": "unreadable(disk)"})
    soup = _soup(client)
    assert "2 lists" in soup.select_one(".nav-item[aria-label]")["aria-label"]
    assert "2 lists are unreadable" in soup.select_one(".head-truth").get_text(" ", strip=True)


def test_the_summary_is_read_once_per_request(monkeypatch):
    """Five stores, five destinations. The frame must not read them per nav item."""
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    calls = []
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_compose_inbox",
                        lambda uid: (calls.append(uid) or compose([], {"asks": "ok"})))
    monkeypatch.setattr(mod, "_tenant", lambda request: ("u1", None))
    app = FastAPI()
    app.include_router(mod.router)
    TestClient(app).get("/inbox")
    assert calls == ["u1"]


def test_no_tenant_is_uncertain_not_a_confident_zero(monkeypatch):
    """An unbound owner cannot be scoped to. That is UNKNOWN, not empty."""
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    monkeypatch.setattr(mod, "_tenant", lambda request: (None, "no owner bound"))
    app = FastAPI()
    app.include_router(mod.router)
    resp = TestClient(app).get("/inbox")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    page = soup.select_one(".page").get_text(" ", strip=True)
    assert "Nothing needs you" not in page
    assert "incomplete" in page
