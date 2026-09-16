"""043 C7 — read-only is stated ONCE, in the frame.

The old console greyed one button at a time, so a person had to discover the
posture by clicking. The banner says it once, at the top of every screen, in
words: *"You are looking at Rob, not driving it."* Per-control "disabled" copy
is then not a second courtesy — it is noise that teaches the reader the first
sentence was not the whole answer.

⚠️ The one thing that IS swapped per screen is the composer, and that is not a
disabled button: it is the one control whose absence would otherwise be
unexplained, so it carries the sentence that explains it and the route to a
seat that can act.
"""
import re

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

PATHS = ["/", "/inbox", "/work", "/money", "/agent"]

#: Copy that would be a SECOND statement of the posture, one control at a time.
_PER_CONTROL = re.compile(
    r"\b(button is disabled|disabled in read.?only|not available in read.?only|"
    r"cannot be used here|greyed out)\b", re.I)


@pytest.fixture()
def client(monkeypatch):
    import webview.chat_open as chat_open
    import webview.pages_new as mod
    from core.surfaces.inbox import compose
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_inbox_summary",
                        lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_public_visitor", lambda request: False)
    monkeypatch.setattr(chat_open, "_tenant", lambda request: ("u1", None))
    monkeypatch.setattr(chat_open, "_recap", lambda uid: [])
    monkeypatch.setattr(chat_open, "_ownership", lambda request, sid: (True, True, False))
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


@pytest.fixture()
def writable_client(monkeypatch):
    import webview.chat_open as chat_open
    import webview.pages_new as mod
    from core.surfaces.inbox import compose
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_inbox_summary",
                        lambda request: compose([], {"asks": "ok"}))
    monkeypatch.setattr(mod, "_public_visitor", lambda request: False)
    monkeypatch.setattr(chat_open, "_tenant", lambda request: ("u1", None))
    monkeypatch.setattr(chat_open, "_recap", lambda uid: [])
    monkeypatch.setattr(chat_open, "_ownership", lambda request, sid: (True, True, False))
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def _soup(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} answered {resp.status_code}"
    return BeautifulSoup(resp.text, "html.parser")


# --- said once -------------------------------------------------------------- #

@pytest.mark.parametrize("path", PATHS)
def test_every_page_states_the_posture_exactly_once(client, path):
    banners = _soup(client, path).select(".banner")
    lead = [b for b in banners
            if "looking at Rob, not driving it" in b.get_text(" ", strip=True)]
    assert len(lead) == 1, f"{path} states read-only {len(lead)} times"


@pytest.mark.parametrize("path", PATHS)
def test_the_banner_is_inside_the_frame_not_the_screen(client, path):
    """It belongs to the shell, so every screen gets it without asking — and a
    screen built next month cannot forget it."""
    soup = _soup(client, path)
    main = soup.select_one("main#main")
    assert main is not None
    assert main.select(".banner"), f"{path} puts the posture outside the frame"


@pytest.mark.parametrize("path", PATHS)
def test_no_page_repeats_the_posture_one_control_at_a_time(client, path):
    text = _soup(client, path).get_text(" ", strip=True)
    assert not _PER_CONTROL.search(text), (
        f"{path} restates read-only per control: {_PER_CONTROL.search(text).group(0)!r}")


@pytest.mark.parametrize("path", PATHS)
def test_no_page_ships_a_disabled_attribute(client, path):
    """A greyed control is the thing the banner replaced."""
    disabled = _soup(client, path).select("[disabled]")
    assert not disabled, f"{path} greys {len(disabled)} control(s)"


# --- the composer is the one exception, and it explains itself -------------- #

def test_the_composer_is_replaced_by_the_sentence_that_explains_it(client):
    soup = _soup(client, "/")
    assert soup.select_one("form.composer") is None
    note = soup.select_one(".composer-note").get_text(" ", strip=True)
    assert "Telegram" in note and "polyrob" in note
    assert "look, not touch" in soup.select_one(".composer-input").get_text(strip=True)


def test_a_writable_console_has_a_composer_and_no_banner(writable_client):
    soup = _soup(writable_client, "/")
    assert soup.select_one("form.composer") is not None
    assert not soup.select(".banner")


@pytest.mark.parametrize("path", PATHS)
def test_a_writable_console_states_nothing(writable_client, path):
    assert not _soup(writable_client, path).select(".banner")


# --- a bound session is a READ, and reads work under read-only -------------- #

def test_a_bound_session_still_loads_its_thread_under_read_only(client):
    """⚠️ The regression this exists for: the script tag was gated on
    ``{% if not read_only %}``, so ``/c/{id}`` was permanently BLANK on the one
    posture prod runs — and every Chats-overlay row links there. The thread is a
    READ; the composer is what read-only removes."""
    soup = _soup(client, "/c/abc123")
    scripts = [s_.get("src") for s_ in soup.select("script[src]")]
    assert "/static/app/chat-open.js" in scripts
    assert soup.select_one("#chat-messages") is not None
    # …and the frame still states the posture exactly once.
    lead = [b for b in soup.select(".banner")
            if "looking at Rob, not driving it" in b.get_text(" ", strip=True)]
    assert len(lead) == 1
    # …with no composer to type into.
    assert soup.select_one("form.composer") is None


def test_the_read_only_cold_open_ships_no_dead_starters(client):
    """⚠️ A starter is a SHORTCUT INTO THE COMPOSER, and read-only has no
    composer — chat-open.js binds them only after it finds the form, so four
    buttons shipped that did nothing at all."""
    soup = _soup(client, "/")
    assert soup.select(".starter") == []
    assert soup.select_one(".open-hero") is not None  # the sentence still lands


def test_a_writable_cold_open_still_offers_them(writable_client):
    assert len(_soup(writable_client, "/").select(".starter")) == 4


def test_the_session_page_tells_the_modules_it_is_read_only(client):
    body = _soup(client, "/c/abc123").select_one("body")
    assert body.get("data-read-only") == "true"


def test_a_writable_console_says_so_on_the_same_attribute(writable_client):
    body = _soup(writable_client, "/c/abc123").select_one("body")
    assert body.get("data-read-only") == "false"


# --- the Inbox states its posture once, and offers no dead buttons ---------- #

def _inbox_soup(a_client, monkeypatch, items):
    import webview.pages_new as mod
    from core.surfaces.inbox import compose
    monkeypatch.setattr(mod, "_inbox_summary",
                        lambda request: compose(items, {"asks": "ok"}))
    return _soup(a_client, "/inbox")


def test_read_only_replaces_the_decision_buttons_with_the_seats_remedy(
        client, monkeypatch):
    from core.surfaces.inbox import Item
    soup = _inbox_soup(client, monkeypatch, [
        Item(kind="ask", id="g7", title="decide me", actions=("fulfill", "reject"))])
    assert not soup.select(".entry-actions button")
    note = soup.get_text(" ", strip=True)
    assert "Telegram" in note and "polyrob" in note


def test_the_remedy_is_stated_once_for_the_page_not_once_per_card(
        client, monkeypatch):
    """One statement, like the composer's. A line under every card is the
    per-control restatement the banner replaced."""
    from core.surfaces.inbox import Item
    soup = _inbox_soup(client, monkeypatch, [
        Item(kind="ask", id=str(i), title=f"decide {i}", actions=("fulfill",))
        for i in range(3)])
    assert len(soup.select(".composer-note")) == 1


def test_a_writable_console_offers_the_buttons(writable_client, monkeypatch):
    from core.surfaces.inbox import Item
    soup = _inbox_soup(writable_client, monkeypatch, [
        Item(kind="ask", id="g7", title="decide me", actions=("fulfill", "reject"))])
    buttons = soup.select(".entry-actions button[data-verb]")
    assert len(buttons) == 2
    assert {b.get("data-verb") for b in buttons} == {"fulfill", "reject"}


def test_the_inbox_loads_the_script_that_binds_its_buttons(writable_client,
                                                            monkeypatch):
    """⚠️ The regression: the buttons were emitted with ``data-verb`` and
    NOTHING bound them, on every posture."""
    from core.surfaces.inbox import Item
    soup = _inbox_soup(writable_client, monkeypatch,
                       [Item(kind="ask", id="g7", title="x", actions=("fulfill",))])
    assert "/static/app/inbox.js" in [s_.get("src") for s_ in soup.select("script[src]")]


# --- the Chats overlay rides on the frame ----------------------------------- #

@pytest.mark.parametrize("path", PATHS)
def test_every_page_carries_the_chats_overlay(writable_client, path):
    soup = _soup(writable_client, path)
    assert soup.select_one("dialog#chats-dialog") is not None
    assert soup.select_one("#chats-open") is not None
    assert soup.select_one("#chats-list") is not None


def test_the_overlay_opens_from_the_head_search_button(writable_client):
    button = _soup(writable_client, "/").select_one("header.head #chats-open")
    assert button is not None
    assert button.get("aria-haspopup") == "dialog"
    assert button.get("aria-label")


def test_the_overlay_carries_every_word_its_script_asks_for(writable_client):
    """The words cross from the copy layer on data attributes, because no 043
    template carries an inline script. A missing one would render as a blank
    row rather than as an error, so it is checked here."""
    from pathlib import Path
    node = _soup(writable_client, "/").select_one("#chats-copy")
    assert node is not None
    js = (Path(__file__).resolve().parents[3]
          / "webview" / "static" / "app" / "chats.js").read_text()
    asked = set(re.findall(r"copy(?:\s*&&\s*copy)?\.([a-z_]+)", js))
    asked |= set(re.findall(r"copy\[`([a-z_]+)\$\{", js))
    have = {k[len("data-"):] for k in node.attrs if k.startswith("data-")}
    # the templated keys (status_/creator_) are checked by prefix
    simple = {a for a in asked if not a.endswith("_")}
    missing = sorted(a for a in simple if a not in have)
    assert not missing, f"chats.js asks for copy the shell does not hand over: {missing}"


def test_the_overlay_needs_no_stylesheet_of_its_own(writable_client):
    """The overlay uses a native <dialog> and inherits the platform's focus
    trap, backdrop and Escape rather than growing a stylesheet: the shell links
    exactly the design-of-record family (fonts, variables, components, style)
    plus the destinations' own app.css — nothing per overlay."""
    html = writable_client.get("/").text
    assert "<dialog" in html
    assert "/static/app/app.css" in html
    assert html.count("<link rel=\"stylesheet\"") == 5
