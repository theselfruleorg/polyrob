"""043 final fix round — an UNBOUND console never says "Nothing needs you."

⚠️ The defect. ``webview/inbox.py::_tenant`` caught ``RuntimeError`` on the
belief that ``webgate.local_owner_id()`` refuses a console with no bound owner.
It does not: it warns ONCE (``webview/webgate.py``) and falls back to the
instance id (``core/instance.py::resolve_owner_user_id``). So
``pages_new._no_tenant_summary`` — the every-source-unreadable body that makes
the page say "incomplete" — was unreachable, and every read was silently scoped
to the literal tenant ``polyrob``.

On the Inbox that renders as **"Nothing needs you."** over the owner's real
queue, on the deployment most likely to be unbound: a read-only monitoring
console beside a headless agent, which is prod's own shape.

``local`` is exempt and must stay exempt: it has exactly one owner by
construction, no auth layer and nobody else to be mis-scoped to.
"""
import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_ambient_owner(monkeypatch):
    """The dev box may have a real owner in its environment."""
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


def _page_client(monkeypatch):
    import webview.pages_new as mod
    monkeypatch.setattr(mod, "_pause_headline", lambda: "")
    monkeypatch.setattr(mod, "_read_only", lambda: False)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app), mod


def _soup(client, path="/inbox"):
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    return BeautifulSoup(resp.text, "html.parser")


# --- the predicate ----------------------------------------------------------- #

def test_an_unbound_public_console_cannot_name_a_tenant(monkeypatch):
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    assert inbox.unbound_console()


def test_the_reason_is_true_of_a_READ_only_console(monkeypatch):
    """⚠️ `webgate.UNBOUND_OWNER_MESSAGE` says "a WRITABLE console needs a bound
    owner", which is untrue of the read-only console this most often fires on —
    and it names an environment variable, which the copy rules keep off a first
    screen. The read path has its own sentence; the write path keeps its own."""
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    reason = inbox.unbound_console()
    assert "whose queue to read" in reason
    assert "writable" not in reason.lower()
    assert "POLYROB_OWNER_USER_ID" not in reason
    # …and the REMEDY, which names it, rides on the API refusal only.
    assert "POLYROB_OWNER_USER_ID" in inbox.UNBOUND_REMEDY


def test_a_bound_console_names_one(monkeypatch):
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    assert inbox.unbound_console() is None


def test_local_is_exempt_because_it_has_exactly_one_owner(monkeypatch):
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "local")
    assert inbox.unbound_console() is None


def test_multitenant_is_exempt_because_it_never_asks_the_binding(monkeypatch):
    """⚠️ Under multitenant the tenant comes from the AUTHENTICATED caller
    (`pages._effective_user_id` reads `request.state.user_id` and 403s without
    one); `local_owner_id` is never consulted. Refusing there would lock every
    authenticated tenant out of their own inbox over a binding their console
    does not use."""
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "multitenant")
    assert inbox.unbound_console() is None


def test_an_authenticated_multitenant_caller_reads_their_own_inbox(
        monkeypatch, tmp_path):
    import webview.inbox as inbox
    import webview.pages as pages
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "multitenant")
    monkeypatch.setattr(webgate, "is_multitenant", lambda: True)
    monkeypatch.setattr(pages.webgate, "is_multitenant", lambda: True)
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))

    app = FastAPI()

    @app.middleware("http")
    async def _authenticate(request, call_next):
        request.state.user_id = "tenant-7"
        return await call_next(request)

    app.include_router(inbox.router)
    resp = TestClient(app).get("/api/webgate/inbox")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "tenant-7"


def test_an_unauthenticated_multitenant_caller_is_still_refused(monkeypatch,
                                                                 tmp_path):
    """The multitenant refusal is `_effective_user_id`'s 403, which is the one
    that was already right — not the owner-binding one."""
    import webview.inbox as inbox
    import webview.pages as pages
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "multitenant")
    monkeypatch.setattr(pages.webgate, "is_multitenant", lambda: True)
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(inbox.router)
    resp = TestClient(app).get("/api/webgate/inbox")
    assert resp.status_code == 403
    assert "tenant identity" in resp.json()["detail"].lower()


def test_a_probe_that_raises_is_a_refusal_too(monkeypatch):
    import webview.inbox as inbox
    import webview.webgate as webgate

    def boom():
        raise RuntimeError("env is gone")

    monkeypatch.setattr(webgate, "posture", boom)
    assert inbox.unbound_console()


def test_local_owner_id_really_does_not_raise(monkeypatch):
    """The belief the dead `except RuntimeError` rested on, pinned as false so
    nobody restores it."""
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "read_only", lambda: False)
    assert webgate.owner_is_bound() is False
    assert isinstance(webgate.local_owner_id(), str)  # warns, never raises


# --- the page ---------------------------------------------------------------- #

def test_an_unbound_console_says_the_list_is_incomplete(monkeypatch):
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    client, mod = _page_client(monkeypatch)
    page = _soup(client).select_one(".page").get_text(" ", strip=True)
    assert "Nothing needs you" not in page
    assert "incomplete" in page


def test_an_unbound_console_draws_the_badge_uncertain(monkeypatch):
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    client, mod = _page_client(monkeypatch)
    soup = _soup(client)
    badge = soup.select_one(".nav-badge")
    assert badge is not None
    assert "is-uncertain" in badge.get("class", [])
    truth = soup.select_one(".head-truth").get_text(" ", strip=True)
    assert "unreadable" in truth


def test_the_page_says_WHY_it_could_not_read_them(monkeypatch):
    """⚠️ The reason lives in `sources[name] = "unreadable(<why>)"` and nothing
    rendered it, so the page named WHICH lists it could not read and never WHY —
    the actionable half."""
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    client, mod = _page_client(monkeypatch)
    banner = _soup(client).select_one(".banner.is-warn").get_text(" ", strip=True)
    assert "no bound owner" in banner
    assert "whose queue to read" in banner


def test_a_mixed_set_of_refusals_invents_no_single_reason(monkeypatch):
    """Five different failures do not collapse into one sentence.

    043 A36: the reason is read from the TYPED ``source_reasons`` mapping
    (``webview.inbox.with_source_reasons``) — the render site no longer slices
    the composer's own prose.
    """
    import webview.pages_new as mod
    from core.surfaces.inbox import compose
    from webview.inbox import with_source_reasons
    body = with_source_reasons(
        compose([], {"asks": "unreadable(locked)", "apps": "unreadable(disk)"}))
    assert mod._shared_refusal(body, ["asks", "apps"]) == ""
    one = with_source_reasons(
        compose([], {"asks": "unreadable(same)", "apps": "unreadable(same)"}))
    assert one["source_reasons"] == {"asks": "same", "apps": "same"}
    assert mod._shared_refusal(one, ["asks", "apps"]) == "same"


def test_every_source_is_named_as_unreadable_not_omitted(monkeypatch):
    import webview.inbox as inbox
    import webview.pages_new as mod
    import webview.webgate as webgate
    from core.surfaces.inbox import SOURCE_LABELS
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")

    class Req:
        pass

    req = Req()
    req.state = type("S", (), {})()
    body = mod._inbox_summary(req)
    assert set(body["sources"]) == set(SOURCE_LABELS)
    assert all(v.startswith("unreadable(") for v in body["sources"].values())
    assert body["uncertain"] is True and body["count"] == 0
    assert inbox  # the predicate lives there; this is the frame reading it


def test_a_bound_console_reads_its_stores_normally(monkeypatch, tmp_path):
    import webview.pages_new as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    client, _mod = _page_client(monkeypatch)
    page = _soup(client).select_one(".page").get_text(" ", strip=True)
    # No stores exist under tmp_path, which is a real "read it, nothing there".
    assert "Nothing needs you" in page
    assert "incomplete" not in page


def test_local_is_unchanged(monkeypatch, tmp_path):
    import webview.pages_new as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "local")
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    client, _mod = _page_client(monkeypatch)
    page = _soup(client).select_one(".page").get_text(" ", strip=True)
    assert "Nothing needs you" in page


# --- the endpoint ------------------------------------------------------------ #

def test_the_endpoint_refuses_rather_than_answering_for_a_made_up_tenant(
        monkeypatch):
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    app = FastAPI()
    app.include_router(inbox.router)
    resp = TestClient(app).get("/api/webgate/inbox")
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"].lower()


def test_a_bound_endpoint_answers(monkeypatch, tmp_path):
    import webview.inbox as inbox
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(inbox.router)
    resp = TestClient(app).get("/api/webgate/inbox")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "owner-1"
