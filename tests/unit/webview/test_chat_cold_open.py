"""043 C6 — chat replaces the config gate.

What a person met on opening the console was a FORM: a model picker, a tool
checkbox grid and a max-steps slider, before a single word had been exchanged
("the cold open is a
config form"). The cold open is now one true sentence made of real numbers, in
Rob's voice, and a composer.

So the tests here are of two kinds:

1. **The sentence is true.** Its numbers come from ``core.recap.build_recap`` —
   the same reader ``/journey`` and Telegram ``/recap`` use, so the two can
   never disagree — and when that read fails the page says so plainly and still
   renders the composer. A cold open that shows a confident zero over an
   unreadable day is the failure this whole programme is about.
2. **The form is gone.** No ``<select`` for a model, no max-steps control, no
   tool checkboxes.
"""
import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.recap import RecapEntry


@pytest.fixture()
def pages_new(monkeypatch):
    import webview.pages_new as mod
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    monkeypatch.setattr(mod, "_inbox_summary",
                        lambda request: compose([], {"asks": "ok"}))
    return mod


@pytest.fixture()
def chat_open(monkeypatch):
    import webview.chat_open as mod
    monkeypatch.setattr(mod, "_tenant", lambda request: ("u1", None))
    monkeypatch.setattr(mod, "_ownership", lambda request, sid: (True, True, False))
    return mod


@pytest.fixture()
def client(pages_new, chat_open):
    app = FastAPI()
    app.include_router(pages_new.router)
    return TestClient(app)


def _entries(monkeypatch, chat_open, entries):
    monkeypatch.setattr(chat_open, "_recap", lambda user_id: entries)


def _soup(client, path="/"):
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    return BeautifulSoup(resp.text, "html.parser")


LEDGER_TEXT = ("Income: $12.00 (2 settled) · spend $3.00 · net $9.00 · "
               "runtime $4.86")


# --- the sentence ----------------------------------------------------------- #

def test_the_hero_is_made_of_the_recaps_own_numbers(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [
        RecapEntry(ts=3.0, kind="episode", text='goal:done $0.10 "a"'),
        RecapEntry(ts=2.0, kind="episode", text='goal:done $0.20 "b"'),
        RecapEntry(ts=1.0, kind="episode", text='goal:failed "c"'),
        RecapEntry(ts=0.0, kind="ledger", text=LEDGER_TEXT, amount=9.0),
    ])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "two goals" in hero or "2 goals" in hero
    assert "$4.86" in hero
    assert "$12.00" in hero


def test_one_goal_is_singular(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [
        RecapEntry(ts=1.0, kind="episode", text="goal:done"),
        RecapEntry(ts=0.0, kind="ledger", text=LEDGER_TEXT),
    ])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "one goal" in hero and "goals" not in hero


def test_no_money_activity_makes_no_money_claim(client, chat_open, monkeypatch):
    """``build_recap`` emits NO ledger entry for an all-zero (or unreadable)
    rollup, so the two are indistinguishable here. The honest answer is to make
    no claim about money at all rather than to print a confident ``$0.00``."""
    _entries(monkeypatch, chat_open,
             [RecapEntry(ts=1.0, kind="episode", text="goal:done")])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "$" not in hero
    assert "one goal" in hero


DEGRADED_TEXT = (LEDGER_TEXT.replace("runtime $4.86", "runtime $0.00")
                 + " ⚠ metering degraded — LLM/API cost metering absent "
                   "(no usage_records table)")


def test_a_degraded_ledger_never_prints_its_zeros_as_money(client, chat_open,
                                                            monkeypatch):
    """⚠️ The defect: the hero rendered "my running cost $0.00, and $12.00 came
    in" over a rollup whose runtime leg was never read — a confident zero next
    to a real figure, which is exactly what the recap's ⚠ note exists to stop.
    The money clause is dropped and the recap's own reason is carried verbatim.
    """
    _entries(monkeypatch, chat_open, [
        RecapEntry(ts=1.0, kind="episode", text="goal:done"),
        RecapEntry(ts=0.0, kind="ledger", text=DEGRADED_TEXT),
    ])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "$0.00" not in hero
    assert "$12.00" not in hero
    assert "one goal" in hero
    assert "metering degraded" in hero
    assert "no usage_records table" in hero


def test_a_clean_ledger_still_prints_both_figures(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [
        RecapEntry(ts=1.0, kind="episode", text="goal:done"),
        RecapEntry(ts=0.0, kind="ledger", text=LEDGER_TEXT),
    ])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "$4.86" in hero and "$12.00" in hero
    assert "not showing" not in hero


def test_the_note_is_read_off_the_recaps_own_entry():
    """A contract test THROUGH build_recap's own rendering, so the day that
    format changes this fails instead of the hero quietly losing the warning."""
    from webview.chat_open import hero_facts
    facts = hero_facts([RecapEntry(ts=0.0, kind="ledger", text=DEGRADED_TEXT)])
    assert facts["ledger_note"].startswith("metering degraded")
    # The figures BEFORE the warning are still parsed — they are simply not
    # printed, so a later renderer can decide differently with the same facts.
    assert facts["income_usd"] == 12.0 and facts["runtime_usd"] == 0.0


def test_a_note_carrying_markup_cannot_become_markup():
    """The hero is rendered |safe, and the note is the one part of it that comes
    from outside the copy layer."""
    from webview.chat_open import hero_sentence
    out = hero_sentence({"goals_done": 0, "runtime_usd": 0.0, "income_usd": 0.0,
                         "ledger_note": '<img src=x onerror="alert(1)">'})
    assert "<img" not in out
    assert "&lt;img" in out


def test_nothing_happened_says_so(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    hero = _soup(client).select_one(".open-hero").get_text(" ", strip=True)
    assert "nothing" in hero.lower()


def test_a_recap_failure_is_honest_and_still_offers_the_composer(
        client, chat_open, monkeypatch):
    def boom(_uid):
        raise RuntimeError("episodes store locked")

    monkeypatch.setattr(chat_open, "_recap", boom)
    soup = _soup(client)
    hero = soup.select_one(".open-hero").get_text(" ", strip=True)
    assert "could not read what I did in the last day" in hero
    assert soup.select_one(".composer-input") is not None


def test_a_missing_tenant_is_honest_too(client, chat_open, monkeypatch):
    monkeypatch.setattr(chat_open, "_tenant", lambda request: (None, "no owner"))
    soup = _soup(client)
    assert "could not read" in soup.select_one(".open-hero").get_text(" ", strip=True)
    assert soup.select_one(".composer-input") is not None


def test_the_hero_reads_the_same_builder_every_other_seat_reads(chat_open):
    """One reader, so /journey, Telegram /recap and this page cannot disagree."""
    import inspect
    assert "build_recap" in inspect.getsource(chat_open._recap)


# --- the form is gone ------------------------------------------------------- #

def test_no_model_picker(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    body = client.get("/").text
    assert "<select" not in body.lower()


def test_no_max_steps_and_no_tool_checkboxes(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    resp = client.get("/")
    assert "max steps" not in resp.text.lower()
    assert 'type="checkbox"' not in resp.text.lower()


def test_four_starters_from_the_copy_layer(client, chat_open, monkeypatch):
    from webview.copy import STRINGS
    _entries(monkeypatch, chat_open, [])
    starters = [b.get_text(" ", strip=True) for b in _soup(client).select(".starter")]
    assert len(starters) == 4
    authored = {v for k, v in STRINGS.items() if k.startswith("chat.starter.")}
    assert set(starters) <= authored


def test_the_composer_posts_nothing_but_the_task(client, chat_open, monkeypatch):
    """The session request carries no model: ``resolve_session_provider_model``
    picks from the operator's runtime config, which is what every other seat
    already does."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[3]
          / "webview" / "static" / "app" / "chat-open.js").read_text()
    assert "/api/task/sessions" in js
    assert "claude-sonnet" not in js
    assert "max_steps" not in js


# --- the frame -------------------------------------------------------------- #

def test_the_cold_open_carries_one_nav_and_one_sources_note(client, chat_open,
                                                            monkeypatch):
    _entries(monkeypatch, chat_open, [])
    soup = _soup(client)
    assert len(soup.select('nav[aria-label="Main"]')) == 1
    assert len(soup.select("p.sources")) == 1


def test_new_is_the_current_destination(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    current = _soup(client).select('[aria-current="page"]')
    assert len(current) == 1
    assert current[0].get_text(" ", strip=True).endswith("New")


# --- a bound session -------------------------------------------------------- #

def test_a_session_page_carries_what_the_chat_modules_read(client, chat_open,
                                                           monkeypatch):
    """The running transcript (043 A14/A16, ``transcript.js``) reads the session
    from ``document.body``'s data attributes, which is why the page carries no
    inline script. That contract is pinned, and so is the security half: the
    page must never hand a VIEWER a writable page. The client look-only gate is
    ``read_only`` (the composer, Stop and Steer render only when it is off);
    ownership itself is enforced by the SERVER on the cancel and message
    endpoints, so the legacy per-viewer ``window`` globals are gone with the
    legacy modules that fed them."""
    from pathlib import Path
    _entries(monkeypatch, chat_open, [])
    soup = _soup(client, "/c/abc123")
    body = soup.select_one("body")
    assert body.get("data-session-id") == "abc123"
    assert body.get("data-is-new") == "false"
    for name in ("data-is-owner", "data-is-authenticated", "data-read-only"):
        assert body.get(name) is not None, name
    for element_id in ("chat-messages", "chat-input", "chat-send-btn"):
        assert soup.select_one(f"#{element_id}") is not None, element_id
    # …and the transcript reads read-only off the body and gates the composer +
    # Stop/Steer on it, so a viewer never gets a writable page.
    app_dir = Path(__file__).resolve().parents[3] / "webview" / "static" / "app"
    tx = (app_dir / "transcript.js").read_text()
    assert "dataset?.readOnly" in tx
    assert "!readOnly" in tx  # the composer gate
    assert "!this.readOnly" in tx  # the Stop/Steer gate on the receipt


def test_a_bound_session_page_says_who_is_looking(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    monkeypatch.setattr(chat_open, "_ownership", lambda request, sid: (True, True, False))
    body = _soup(client, "/c/abc123").select_one("body")
    assert body.get("data-is-owner") == "true"
    assert body.get("data-is-authenticated") == "true"


def test_the_owner_of_a_session_counts_as_authenticated(monkeypatch):
    """The ``local`` posture has no auth layer at all, and its operator IS the
    owner. Reporting them unauthenticated would put ``chat.js`` into look-only
    mode on the one posture that has nobody else to protect the session from."""
    import webview.chat_open as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "requires_owner_login", lambda: True)
    monkeypatch.setattr("webview.server._check_session_ownership",
                        lambda request, sid: (True, "local", "local"))
    monkeypatch.setattr("utils.auth_utils.is_authenticated", lambda r: False)
    assert mod._ownership(object(), "s1") == (True, True, False)


def test_local_resolves_ownership_without_the_probe(monkeypatch):
    """One owner by construction, so a missing check does not make it unknown."""
    import webview.chat_open as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "requires_owner_login", lambda: False)
    assert mod._ownership(object(), "s1") == (True, True, False)


def test_a_failed_ownership_probe_is_UNKNOWN_not_gone(client, chat_open,
                                                       monkeypatch):
    """⚠️ A 404 asserts the session is gone and the thread asserts the caller
    may look. A probe that raised knows neither."""
    _entries(monkeypatch, chat_open, [])

    def boom(request, sid):
        raise RuntimeError("the session store did not answer")

    monkeypatch.setattr(chat_open, "_ownership", boom)
    resp = client.get("/c/abc123")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    assert "could not check who owns this session" in text
    assert "not the same as it being gone" in text
    # …and nothing that would act on a session the check never cleared.
    assert soup.select_one("#chat-messages") is None
    assert soup.select_one("form.composer") is None
    assert "/static/app/chat-open.js" not in [s_.get("src")
                                              for s_ in soup.select("script[src]")]


def test_the_probe_itself_reports_unknown_when_the_check_is_unreachable(monkeypatch):
    import webview.chat_open as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "requires_owner_login", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "webview.server", None)
    assert mod._ownership(object(), "s1") == (False, False, True)


def test_a_non_owner_does_not_get_the_session_page(client, chat_open, monkeypatch):
    """The owner's seat. What a caller may not have is ABSENT, not
    access-denied — and a 404 declines to confirm the session exists."""
    _entries(monkeypatch, chat_open, [])
    monkeypatch.setattr(chat_open, "_ownership", lambda request, sid: (False, True, False))
    assert client.get("/c/abc123").status_code == 404


def test_the_cold_open_is_never_gated_on_session_ownership(client, chat_open,
                                                           monkeypatch):
    """There is no session to own yet."""
    _entries(monkeypatch, chat_open, [])

    def boom(request, sid):  # pragma: no cover - must not be reached
        raise AssertionError("the cold open asked about a session")

    monkeypatch.setattr(chat_open, "_ownership", boom)
    assert client.get("/").status_code == 200


def test_ownership_fails_closed_when_the_shared_check_is_unreachable(monkeypatch):
    """A console that cannot reach the ONE check treats the caller as NOT the
    owner on any posture that has authentication at all."""
    import webview.chat_open as mod
    import webview.webgate as webgate

    monkeypatch.setattr(webgate, "requires_owner_login", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "webview.server", None)
    assert mod._ownership(object(), "s1")[0] is False


def test_the_session_page_has_no_hero(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    assert _soup(client, "/c/abc123").select_one(".open-hero") is None


def test_the_cold_open_is_marked_new(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    assert _soup(client).select_one("body").get("data-is-new") == "true"


def test_a_session_id_that_is_not_one_is_refused(client, chat_open, monkeypatch):
    _entries(monkeypatch, chat_open, [])
    assert client.get("/c/..%2Fetc").status_code in (400, 404)
    assert client.get("/c/a b").status_code in (400, 404)


# --- the public status page still answers a stranger ------------------------ #

def test_an_unauthenticated_stranger_on_a_public_posture_gets_the_status_page(
        client, chat_open, monkeypatch):
    """``/`` on own_ops is ALSO the public status page. Replacing the console
    index must not put the owner's chat in front of a stranger."""
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_public_visitor", lambda request: True)
    soup = _soup(client)
    assert soup.select_one(".composer-input") is None
    assert soup.select_one(".open-hero") is None


def test_the_visitor_rule_is_the_posture_rule_not_a_stub(monkeypatch):
    """The real predicate, over the real posture — the property
    ``tests/unit/webview/conftest.py`` flags as now only covered on the legacy
    side. ``local`` is never a stranger; ``own_ops`` without an authenticated
    caller always is."""
    import webview.pages_new as mod

    class Req:
        pass

    monkeypatch.setenv("POLYROB_POSTURE", "local")
    assert mod._public_visitor(Req()) is False

    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setattr("utils.auth_utils.is_authenticated", lambda r: False)
    assert mod._public_visitor(Req()) is True
    monkeypatch.setattr("utils.auth_utils.is_authenticated", lambda r: True)
    assert mod._public_visitor(Req()) is False


def test_a_visitor_probe_that_raises_is_treated_as_a_stranger(monkeypatch):
    """Fail-CLOSED. An auth probe that blows up must not hand the owner's chat
    to whoever asked."""
    import webview.pages_new as mod

    def boom(_request):
        raise RuntimeError("auth layer is down")

    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setattr("utils.auth_utils.is_authenticated", boom)
    assert mod._public_visitor(object()) is True


# --- the legacy index steps aside ------------------------------------------- #

def _roots(app):
    """Every handler the app serves at ``/``, seen THROUGH ``include_router``.

    ⚠️ ``app.routes`` is not the list of routes: since FastAPI 0.141
    ``include_router`` appends one opaque wrapper holding the real routes on
    ``.original_router`` (C3). A shallow scan here would report the new index as
    missing and pass the day it stopped being registered.
    """
    found = []

    def walk(routes, depth=0):
        if depth > 6:
            return
        for route in routes or ():
            if getattr(route, "path", None) == "/":
                found.append(route)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", ()), depth + 1)
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested, depth + 1)

    walk(getattr(app, "routes", ()))
    return found


def test_mount_removes_the_legacy_console_index(pages_new, monkeypatch):
    monkeypatch.delenv("WEBVIEW_UI", raising=False)
    app = FastAPI()

    @app.get("/", name="index")
    async def legacy_index():  # pragma: no cover - never called
        return {"legacy": True}

    pages_new.mount(app)
    names = [getattr(r, "name", None) for r in _roots(app)]
    assert names == ["console_new"]


def test_mount_removes_only_the_named_legacy_index(pages_new, monkeypatch):
    """A route at ``/`` that is NOT the legacy console index is not ours to
    delete — the filter is on the name as well as the path."""
    monkeypatch.delenv("WEBVIEW_UI", raising=False)
    app = FastAPI()

    @app.get("/", name="something_else")
    async def other():  # pragma: no cover - never called
        return {"other": True}

    pages_new.mount(app)
    assert [getattr(r, "name", None) for r in _roots(app)] == ["something_else"]
