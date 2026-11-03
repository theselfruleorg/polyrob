"""098 CL-11: X signup progress — unreadable is not absent, an obstacle step
stays not-done, one password copy, a locked read-merge-write."""
import pytest

pytest.importorskip("polyrob_x")

import threading

from polyrob_x.x_browser.session_store import XSessionStore, XSessionUndecryptable
from polyrob_x.x_browser.signup import Obstacle, SignupFlow, SignupPaused


@pytest.fixture()
def key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POLYROB_AGENT_EMAIL", "robbot@agentmail.to")
    import core.security.encryption as enc
    monkeypatch.delattr(enc, "_encryption_instance", raising=False)


def _rotate_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import core.security.encryption as enc
    monkeypatch.delattr(enc, "_encryption_instance", raising=False)


class Mail:
    async def wait_for_code(self, timeout=120):
        return "123456"


class Driver:
    def __init__(self, probes=()):
        self.probes = list(probes)
        self.calls = []

    async def open_signup(self):
        self.calls.append("open")

    async def fill_details(self, *a):
        self.calls.append("details")

    async def request_code(self):
        self.calls.append("request_code")

    async def enter_code(self, code):
        self.calls.append("enter_code")

    async def set_password(self, pw):
        self.calls.append("password")

    async def set_handle_and_profile(self, h, b):
        self.calls.append("profile")
        return {}

    async def current_handle(self):
        return "robbot"

    async def export_storage_state(self):
        return {"cookies": []}

    async def probe(self):
        return self.probes.pop(0) if self.probes else "ok"


class Ident:
    name, dob, handle, disclosure = "Nova", "2000-01-01", "robbot", "bot"


def _flow(tmp_path, driver):
    path = tmp_path / ".x_session.json"
    return SignupFlow(driver=driver, mail=Mail(), store=XSessionStore(path=path),
                      progress=XSessionStore(path=path, provider="x_signup"),
                      identity=Ident(), user_id="u1")


def test_undecryptable_save_does_not_drop_password(tmp_path, key, monkeypatch):
    s = XSessionStore(path=tmp_path / ".x_session.json")
    s.save("u1", storage_state={"c": 1}, password="pw", handle="h")
    _rotate_key(monkeypatch)
    assert s.load("u1") is None
    with pytest.raises(XSessionUndecryptable):
        s.save("u1", password="new")


@pytest.mark.asyncio
async def test_undecryptable_login_blocks_new_signup(tmp_path, key, monkeypatch):
    s = XSessionStore(path=tmp_path / ".x_session.json")
    s.save("u1", storage_state={"c": 1}, password="pw", handle="h")
    _rotate_key(monkeypatch)
    assert s.load("u1") is None
    d = Driver()
    with pytest.raises(SignupPaused) as ei:
        await _flow(tmp_path, d).run()
    assert ei.value.obstacle is Obstacle.BLOCKED and "decrypted" in ei.value.prompt
    assert d.calls == []


@pytest.mark.asyncio
async def test_obstacle_marks_step_not_done(tmp_path, key):
    d = Driver(probes=["ok", "arkose_challenge"])   # open ok, details -> CAPTCHA
    f = _flow(tmp_path, d)
    with pytest.raises(SignupPaused):
        await f.run()
    rec = f.progress.load("u1")
    assert rec["signup_step"] == "details" and rec["step_done"] is False
    # resume while the wall is still there: pause again, never march past it
    d2 = Driver(probes=["arkose_challenge"])
    with pytest.raises(SignupPaused):
        await _flow(tmp_path, d2).run(resume=True)
    assert "request_code" not in d2.calls
    # the owner solved it: resume continues after details
    d3 = Driver()
    await _flow(tmp_path, d3).run(resume=True)
    assert d3.calls[0] == "request_code"


@pytest.mark.asyncio
async def test_password_only_in_login_record(tmp_path, key):
    d = Driver(probes=["ok", "arkose_challenge"])
    f = _flow(tmp_path, d)
    with pytest.raises(SignupPaused):
        await f.run()
    assert f.store.load("u1")["password"]
    assert "password" not in (f.progress.load("u1") or {})


def test_old_progress_password_is_migrated(tmp_path, key):
    path = tmp_path / ".x_session.json"
    XSessionStore(path=path, provider="x_signup").save("u1", password="legacy")
    f = _flow(tmp_path, Driver())
    assert f._ensure_password() == "legacy"
    assert f.store.load("u1")["password"] == "legacy"
    assert "password" not in f.progress.load("u1")


def test_two_instances_update_concurrently_keep_both_fields(tmp_path, key):
    path = tmp_path / ".x_session.json"
    a, b = XSessionStore(path=path), XSessionStore(path=path)
    a.save("u1", handle="h")
    b.load("u1")  # b holds a snapshot
    errs = []

    def w(store, **kw):
        try:
            for _ in range(10):
                store.save("u1", **kw)
        except Exception as e:  # pragma: no cover
            errs.append(e)
    t1 = threading.Thread(target=w, args=(a,), kwargs={"password": "pw"})
    t2 = threading.Thread(target=w, args=(b,), kwargs={"storage_state": {"s": 1}})
    t1.start(); t2.start(); t1.join(); t2.join()
    rec = XSessionStore(path=path).load("u1")
    assert not errs
    assert rec["password"] == "pw" and rec["storage_state"] == {"s": 1} and rec["handle"] == "h"


class BlankDriver(Driver):
    """A driver in a NEW process: its page never left about:blank."""

    def __init__(self, probes=()):
        super().__init__(probes)
        self.url = "about:blank"
        self.imported = None

    async def current_url(self):
        return self.url

    async def page_is_blank(self):
        return self.url == "about:blank"

    async def import_storage_state(self, state, *, url=""):
        self.imported = (state, url)
        self.calls.append("import")
        self.url = url or "https://x.com/i/flow/signup"

    async def export_storage_state(self):
        return {"cookies": [{"name": "auth", "value": "v", "domain": ".x.com", "path": "/"}]}


@pytest.mark.asyncio
async def test_cross_process_resume_restores_storage_state(tmp_path, key):
    d = BlankDriver(probes=["ok", "ok", "arkose_challenge"])  # stop at request_code
    d.url = "https://x.com/i/flow/signup"
    with pytest.raises(SignupPaused):
        await _flow(tmp_path, d).run()
    # another process: a fresh browser on about:blank
    d2 = BlankDriver()
    await _flow(tmp_path, d2).run(resume=True)
    assert d2.calls[0] == "import", "the saved state loads before any probe or step"
    state, url = d2.imported
    assert state["cookies"][0]["name"] == "auth" and url == "https://x.com/i/flow/signup"
    # request_code stopped on a wall; the imported page is clear, so it continues
    assert d2.calls[1] == "enter_code"


@pytest.mark.asyncio
async def test_same_page_resume_does_not_reimport(tmp_path, key):
    d = BlankDriver(probes=["ok", "arkose_challenge"])
    d.url = "https://x.com/i/flow/signup"
    f = _flow(tmp_path, d)
    with pytest.raises(SignupPaused):
        await f.run()
    await f.run(resume=True)  # the owner solved it in the open window
    assert "import" not in d.calls


@pytest.mark.asyncio
async def test_blank_page_resume_without_saved_state_refuses(tmp_path, key):
    path = tmp_path / ".x_session.json"
    XSessionStore(path=path, provider="x_signup").save(
        "u1", extra={"signup_step": "details", "step_done": True, "signup_state": {}})
    d = BlankDriver()
    with pytest.raises(SignupPaused) as ei:
        await _flow(tmp_path, d).run(resume=True)
    assert "first step" in ei.value.prompt and d.calls == []
