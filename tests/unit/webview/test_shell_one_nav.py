"""043 C3 — rule 1 of the design system, held to by the product.

*One nav component. It rotates axis at 768 px. It never forks.*
(``docs/design/040/README.md``, owner decision 2026-09-13.)

The console today presents **15 flat top-level destinations**; the mockups
present five. The regression this file exists to prevent is not "the nav looks
wrong" — it is the nav quietly becoming five slightly different navs, one per
page, which is how it got to fifteen. So the assertion is byte-identical
markup across every destination, with only the two genuinely per-screen things
blanked: the current marker and the Inbox count.

The same invariant is checked over the MOCKUPS by ``webview/dev/nav.test.js``,
so the picture and the product are held to one bar.

The app is built here rather than imported from ``webview.server`` (the pattern
of ``test_invoices_settle.py::_app``): the router is the contract, and it must
be mountable with nothing else in the process.
"""
import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

PATHS = ["/", "/inbox", "/work", "/money", "/agent"]
LABELS = ["New", "Inbox", "Work", "Money", "Agent"]


@pytest.fixture()
def pages_new(monkeypatch):
    import webview.pages_new as mod
    # A shell test must not depend on the host's pause record or posture.
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_inbox_state", lambda request: (0, False, 0))
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    # 043 C5: /inbox is a real screen now, so the frame test must not read the
    # developer's own five stores to render it. One clean, empty composition.
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_inbox_summary",
                        lambda request: compose([], {"asks": "ok"}))
    return mod


@pytest.fixture()
def client(pages_new):
    app = FastAPI()
    app.include_router(pages_new.router)
    return TestClient(app)


def _soup(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} answered {resp.status_code}"
    return BeautifulSoup(resp.text, "html.parser")


def _nav(soup):
    navs = soup.select('nav[aria-label="Main"]')
    assert len(navs) == 1, f"expected one main nav, found {len(navs)}"
    return navs[0]


def _skeleton(nav):
    """The nav with the two genuinely per-screen things removed: the current
    marker and the Inbox count. What is left is the component, and it must be
    the same component on every destination and at every count."""
    copy = BeautifulSoup(str(nav), "html.parser")
    for badge in copy.select(".nav-badge"):
        badge.decompose()
    for item in copy.select("a.nav-item"):
        item.attrs.pop("aria-current", None)
        item.attrs.pop("aria-label", None)
    return str(copy)


# --- the component ---------------------------------------------------------- #

@pytest.mark.parametrize("path", PATHS)
def test_every_destination_renders(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_exactly_one_main_nav(client, path):
    _nav(_soup(client, path))


@pytest.mark.parametrize("path", PATHS)
def test_five_destinations_in_order(client, path):
    items = _nav(_soup(client, path)).select("a.nav-item")
    assert [a.select_one(".nav-label").get_text() for a in items] == LABELS
    assert [a["href"] for a in items] == PATHS


def test_the_nav_is_byte_identical_on_every_destination(client):
    first = _skeleton(_nav(_soup(client, PATHS[0])))
    for path in PATHS[1:]:
        assert _skeleton(_nav(_soup(client, path))) == first, f"{path} forked the nav"


@pytest.mark.parametrize("path,label", list(zip(PATHS, LABELS)))
def test_the_current_destination_is_marked_once(client, path, label):
    nav = _nav(_soup(client, path))
    current = nav.select('[aria-current="page"]')
    assert len(current) == 1
    assert current[0].select_one(".nav-label").get_text() == label


# --- the frame -------------------------------------------------------------- #

@pytest.mark.parametrize("path", PATHS)
def test_a_skip_link_precedes_one_main(client, path):
    html = client.get(path).text
    soup = BeautifulSoup(html, "html.parser")
    skip = soup.select("a.skip-link")
    assert len(skip) == 1, "the nav precedes main in DOM order at both sizes"
    assert skip[0]["href"] == "#main"
    mains = soup.select("main#main")
    assert len(mains) == 1
    assert html.index("skip-link") < html.index('id="main"')


@pytest.mark.parametrize("path", PATHS)
def test_the_page_has_one_h1(client, path):
    assert len(_soup(client, path).select("h1")) == 1


@pytest.mark.parametrize("path", PATHS)
def test_the_shell_reaches_no_network(client, path):
    """The whole point of vendoring the faces (C2)."""
    html = client.get(path).text
    assert "http://" not in html and "https://" not in html
    assert "/static/app/app.css" in html


# --- the count -------------------------------------------------------------- #

def test_no_badge_when_nothing_waits(client):
    nav = _nav(_soup(client, "/inbox"))
    inbox = nav.select("a.nav-item")[1]
    assert inbox.select_one(".nav-badge") is None
    assert not inbox.get("aria-label")


def _client_with(pages_new, monkeypatch, *, inbox=(0, False, 0), read_only=False):
    monkeypatch.setattr(pages_new, "_inbox_state", lambda request: inbox)
    monkeypatch.setattr(pages_new, "_read_only", lambda: read_only)
    app = FastAPI()
    app.include_router(pages_new.router)
    return TestClient(app)


def test_the_count_is_aria_hidden_and_rides_on_the_label(pages_new, monkeypatch):
    client = _client_with(pages_new, monkeypatch, inbox=(2, False, 0))
    inbox = _nav(_soup(client, "/work")).select("a.nav-item")[1]
    badge = inbox.select_one(".nav-badge")
    assert badge is not None and badge.get_text() == "2"
    assert badge["aria-hidden"] == "true"
    # Correction 13: the badge sat inside the link TEXT, so the Inbox link
    # announced "2 Inbox". It still sits inside the link in the DOM (CSS places
    # it), but it is aria-hidden, and the accessible name is the aria-label —
    # which reads as a sentence and leads with the destination.
    assert inbox["aria-label"].startswith("Inbox")
    assert "2" in inbox["aria-label"]


def test_an_uncertain_count_is_drawn_as_uncertain(pages_new, monkeypatch):
    client = _client_with(pages_new, monkeypatch, inbox=(1, True, 1))
    inbox = _nav(_soup(client, "/work")).select("a.nav-item")[1]
    badge = inbox.select_one(".nav-badge")
    assert badge.get_text() == "1+"
    assert "is-uncertain" in badge.get("class", [])
    # The badge may never claim more certainty than the list behind it.
    assert inbox["aria-label"] != "Inbox, 1 waiting"
    assert "unread" in inbox["aria-label"] or "unreadable" in inbox["aria-label"]


def test_the_label_counts_the_unreadable_lists_too(pages_new, monkeypatch):
    """Saying "one list" over three of them is a small lie in the direction of
    "it is fine" — the one direction this badge may never lean."""
    client = _client_with(pages_new, monkeypatch, inbox=(2, True, 3))
    soup = _soup(client, "/work")
    label = _nav(soup).select("a.nav-item")[1]["aria-label"]
    assert "3 lists" in label
    assert "one list" not in label
    truth = soup.select_one(".head-truth").get_text(" ", strip=True)
    assert "3 lists are unreadable" in truth


def test_one_unreadable_list_is_still_singular(pages_new, monkeypatch):
    client = _client_with(pages_new, monkeypatch, inbox=(2, True, 1))
    soup = _soup(client, "/work")
    assert "one list" in _nav(soup).select("a.nav-item")[1]["aria-label"]
    assert "one list is unreadable" in soup.select_one(".head-truth").get_text(" ", strip=True)


def test_the_nav_still_does_not_fork_when_the_count_is_uncertain(pages_new, monkeypatch):
    plain = _client_with(pages_new, monkeypatch, inbox=(0, False, 0))
    baseline = _skeleton(_nav(_soup(plain, "/work")))
    counted = _client_with(pages_new, monkeypatch, inbox=(2, False, 0))
    assert _skeleton(_nav(_soup(counted, "/work"))) == baseline
    uncertain = _client_with(pages_new, monkeypatch, inbox=(1, True, 1))
    assert _skeleton(_nav(_soup(uncertain, "/work"))) == baseline


# --- the read-only frame ---------------------------------------------------- #

def test_read_only_is_stated_once_in_the_frame(pages_new, monkeypatch):
    client = _client_with(pages_new, monkeypatch, read_only=True)
    banners = _soup(client, "/work").select(".banner")
    assert len(banners) == 1, "state the mode once, not one greyed button at a time"


def test_no_banner_when_the_console_can_act(client):
    assert _soup(client, "/work").select(".banner") == []


# --- the mount contract ----------------------------------------------------- #

def test_mount_registers_the_destinations(pages_new):
    # 043 phase 5: the new console is the ONLY console — mount always mounts.
    app = FastAPI()
    pages_new.mount(app)
    assert set(PATHS) <= pages_new.served_paths(app)


def test_mount_never_shadows_a_route_the_app_already_has(pages_new, monkeypatch):
    """The legacy console index is registered at ``/``.

    Starlette matches the FIRST registered route, so including a duplicate is
    not an error — it is a route that silently never runs. Mounting must not
    depend on being called in the right order relative to it.
    """
    monkeypatch.delenv("WEBVIEW_UI", raising=False)
    app = FastAPI()

    @app.get("/")
    async def legacy():  # pragma: no cover - identity only
        return {"legacy": True}

    pages_new.mount(app)
    assert TestClient(app).get("/").json() == {"legacy": True}
    assert TestClient(app).get("/inbox").status_code == 200


def test_served_paths_sees_through_an_included_router(pages_new):
    """The guard's own footing.

    ``app.routes`` is not the list of paths: since FastAPI 0.141
    ``include_router`` appends ONE opaque object holding the real routes, so a
    shallow scan reports a console with a dozen routers as serving four paths —
    and a clash check built on it would be a check that always passes.
    """
    from fastapi import APIRouter

    app = FastAPI()
    inner = APIRouter()

    @inner.get("/deep/route")
    async def deep():  # pragma: no cover - identity only
        return {}

    app.include_router(inner)
    assert "/deep/route" in pages_new.served_paths(app)
    # Older supported FastAPI versions flatten include_router. Exercise the
    # opaque-wrapper shape explicitly as well, without pinning the installed version.
    from types import SimpleNamespace
    wrapped = SimpleNamespace(routes=[SimpleNamespace(original_router=inner)])
    assert "/deep/route" in pages_new.served_paths(wrapped)
