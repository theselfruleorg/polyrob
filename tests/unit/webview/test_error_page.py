"""070 W0.18 + E.34 — one handler for every HTTP error.

An unmatched route used to answer raw ``{"detail":"Not Found"}`` (Starlette's own
``HTTPException`` never reached the console handler). Now a page gets the
console's error page in plain words, an API path still gets JSON, and the raw
detail sits behind "Show details".
"""
import importlib

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import agents.task.path as pth
    pth.reset_path_manager()
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    yield TestClient(srv._fastapi)
    for key in ("POLYROB_POSTURE", "ENV"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(wg)
    importlib.reload(srv)


def _soup(resp):
    return BeautifulSoup(resp.text, "html.parser")


# --- W0.18 ------------------------------------------------------------------ #

def test_unknown_path_renders_the_error_page(client):
    resp = client.get("/nope-404")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert _soup(resp).select_one(".error-screen") is not None


def test_unknown_api_path_is_json(client):
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["detail"] == "Not Found"


def test_raised_404_still_renders(client):
    resp = client.get("/c/a b")
    assert resp.status_code == 404
    assert _soup(resp).select_one(".error-screen") is not None


# --- E.34 ------------------------------------------------------------------- #

def test_an_unknown_page_is_the_error_page_not_json(client):
    resp = client.get("/no-such-page")
    soup = _soup(resp)
    text = soup.get_text(" ", strip=True)
    assert "Something went wrong." in text
    assert "This page does not exist." in text
    assert "Check the address and try again." in text
    assert soup.select_one('a.action-link[href="/"]').get_text(strip=True) == "Go to Chat"
    assert "SYSTEM ERROR" not in resp.text and "RETURN TO HOME" not in resp.text
    assert soup.title.get_text(strip=True) == "Something went wrong – Rob"


def test_an_unknown_api_path_is_still_json(client):
    resp = client.get("/api/no-such-thing")
    assert resp.json() == {"detail": "Not Found", "error": "Not Found"}


def test_the_chat_hint_is_only_on_chat_paths(client):
    hint = "This chat may have been deleted."
    assert hint not in client.get("/no-such-page").text
    assert hint in client.get("/c/a b").text


def test_the_raw_detail_is_behind_details(client):
    soup = _soup(client.get("/no-such-page"))
    details = soup.select_one("details")
    assert details is not None and details.get("open") is None
    assert details.select_one("summary").get_text(strip=True) == "Show details"
    assert "Not Found" in details.get_text(" ", strip=True)
    assert "Not Found" not in soup.select_one(".error-message").get_text()


def test_each_status_class_has_its_own_lead():
    from types import SimpleNamespace
    from webview.copy import t
    from webview.error_page import error_context, lead_key
    assert lead_key(404) == "error.not_found"
    assert lead_key(401) == lead_key(403) == "error.forbidden"
    assert lead_key(500) == lead_key(503) == "error.server"
    assert lead_key(418) == "error.other"
    req = SimpleNamespace(url=SimpleNamespace(path="/x"))
    assert error_context(req, 503, "boom")["lead"] == t("error.server")


def test_the_layout_nav_says_the_shell_words(client):
    """The pages outside the shell carry the same nav words as the shell."""
    soup = _soup(client.get("/no-such-page"))
    labels = [a.get_text(strip=True) for a in soup.select(".terminal-nav a, nav a")]
    assert "Chat" in labels and "Rob" in labels
    assert "New" not in labels and "Agent" not in labels
