"""X signup state machine — obstacle taxonomy, resume, one-account guard (Task 11)."""
import pytest

from tools.x_browser.signup import (
    Obstacle,
    SignupFlow,
    SignupPaused,
    classify_page,
)


class FakeStore:
    def __init__(self, existing=None):
        self._data = dict(existing or {})

    def load(self, uid):
        return self._data.get(uid)

    def exists(self, uid):
        return uid in self._data

    def save(self, uid, *, storage_state=None, password=None, handle=None, extra=None):
        rec = self._data.setdefault(uid, {})
        if storage_state is not None:
            rec["storage_state"] = storage_state
        if password is not None:
            rec["password"] = password
        if handle is not None:
            rec["handle"] = handle
        if extra:
            rec.update(extra)

    def delete(self, uid):
        self._data.pop(uid, None)


class FakeMail:
    def __init__(self, code="123456", arrive_on=1):
        self.code = code
        self.arrive_on = arrive_on
        self.polls = 0

    async def wait_for_code(self, timeout=120):
        self.polls += 1
        if self.polls >= self.arrive_on:
            return self.code
        return None


class FakeDriver:
    """Scripted page states: a list of probe() strings the flow will encounter."""

    def __init__(self, probes=None):
        self._probes = list(probes or [])
        self.calls = []
        self.typed_password = None
        self.bio = None

    async def open_signup(self):
        self.calls.append("open")

    async def fill_details(self, name, email, dob):
        self.calls.append(("fill", name, email))

    async def request_code(self):
        self.calls.append("request_code")

    async def enter_code(self, code):
        self.calls.append(("code", code))

    async def set_password(self, password):
        self.typed_password = password
        self.calls.append("password")

    async def set_handle_and_profile(self, handle, bio):
        self.bio = bio
        self.calls.append(("profile", handle))

    async def current_handle(self):
        return "robbot"

    async def export_storage_state(self):
        return {"cookies": [{"name": "auth_token", "value": "t"}]}

    async def probe(self):
        return self._probes.pop(0) if self._probes else "ok"


class Identity:
    name = "Rob"
    dob = "2000-01-01"
    handle = "robbot"
    disclosure = "Automated account (bot) operated by Owner."


@pytest.fixture(autouse=True)
def _agent_email(monkeypatch):
    # The flow refuses to register with NO email (it used to fill an empty
    # field and fail late); the fakes here model a provisioned agent inbox.
    monkeypatch.setenv("POLYROB_AGENT_EMAIL", "robbot@agentmail.to")


def _flow(driver, store=None, mail=None, progress=None):
    return SignupFlow(
        driver=driver,
        mail=mail or FakeMail(),
        store=store or FakeStore(),
        progress=progress or FakeStore(),
        identity=Identity(),
        user_id="u1",
    )


# --- classify_page --------------------------------------------------------

def test_classify_maps_states():
    assert classify_page("arkose_challenge") is Obstacle.INTERACTIVE_CHALLENGE
    assert classify_page("phone_required") is Obstacle.PHONE_REQUIRED
    assert classify_page("account_suspended") is Obstacle.BLOCKED
    assert classify_page("ok") is None
    # Unknown state defaults to a human hand-off (safe).
    assert classify_page("some_new_wall") is Obstacle.INTERACTIVE_CHALLENGE


# --- happy path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_completes_and_saves():
    driver = FakeDriver(probes=["ok", "ok", "ok"])
    store = FakeStore()
    flow = _flow(driver, store=store)
    result = await flow.run()
    assert result.handle == "robbot"
    rec = store.load("u1")
    assert rec["storage_state"]["cookies"]
    assert rec["password"]  # generated + stored
    assert "bot" in driver.bio.lower()  # disclosure present


@pytest.mark.asyncio
async def test_password_stored_before_typed():
    order = []
    driver = FakeDriver(probes=["ok", "ok", "ok"])

    class RecStore(FakeStore):
        def save(self, uid, **kw):
            if kw.get("password"):
                order.append("stored")
            super().save(uid, **kw)

    orig_set = driver.set_password

    async def set_password(pw):
        order.append("typed")
        await orig_set(pw)
    driver.set_password = set_password

    await _flow(driver, store=RecStore()).run()
    assert order[:2] == ["stored", "typed"]


# --- code arrival ---------------------------------------------------------

@pytest.mark.asyncio
async def test_code_arrives_on_second_poll():
    driver = FakeDriver(probes=["ok", "ok", "ok"])
    mail = FakeMail(code="654321", arrive_on=2)
    await _flow(driver, mail=mail).run()
    assert ("code", "654321") in driver.calls


# --- obstacles ------------------------------------------------------------

@pytest.mark.asyncio
async def test_captcha_pauses_and_persists_progress():
    driver = FakeDriver(probes=["arkose_challenge"])
    progress = FakeStore()
    flow = _flow(driver, progress=progress)
    with pytest.raises(SignupPaused) as ei:
        await flow.run()
    assert ei.value.obstacle is Obstacle.INTERACTIVE_CHALLENGE
    assert progress.exists("u1")  # resumable progress written


@pytest.mark.asyncio
async def test_phone_required_pauses():
    driver = FakeDriver(probes=["phone_required"])
    with pytest.raises(SignupPaused) as ei:
        await _flow(driver).run()
    assert ei.value.obstacle is Obstacle.PHONE_REQUIRED


@pytest.mark.asyncio
async def test_blocked_pauses():
    driver = FakeDriver(probes=["account_suspended"])
    with pytest.raises(SignupPaused) as ei:
        await _flow(driver).run()
    assert ei.value.obstacle is Obstacle.BLOCKED


# --- guards + resume ------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_account_refused():
    driver = FakeDriver(probes=["ok", "ok", "ok"])
    store = FakeStore(existing={"u1": {"handle": "already", "storage_state": {}}})
    with pytest.raises(SignupPaused) as ei:
        await _flow(driver, store=store).run()
    assert ei.value.obstacle is Obstacle.BLOCKED
    assert "already" in ei.value.prompt.lower() or "exists" in ei.value.prompt.lower()


@pytest.mark.asyncio
async def test_resume_skips_completed_steps():
    # Progress recorded past the details step -> resume must not re-open the form.
    progress = FakeStore(existing={"u1": {"signup_step": "wait_code",
                                          "signup_state": {}}})
    driver = FakeDriver(probes=["ok", "ok"])
    flow = _flow(driver, progress=progress)
    await flow.run(resume=True)
    assert "open" not in driver.calls  # did not restart from scratch


# --- pre-flight refusals (2026-09-17) --------------------------------------

@pytest.mark.asyncio
async def test_no_agent_email_refuses_before_any_page(monkeypatch):
    """No address = no signup. Refuse up front and name the remedy instead of
    filling an empty email and pausing steps later on a misleading reason."""
    monkeypatch.delenv("POLYROB_AGENT_EMAIL", raising=False)
    monkeypatch.delenv("GMAIL_EMAIL", raising=False)
    monkeypatch.setattr("core.instance.resolve_agent_email", lambda *a, **k: None)
    driver = FakeDriver(probes=["ok", "ok", "ok"])
    with pytest.raises(SignupPaused) as ei:
        await _flow(driver).run()
    assert ei.value.obstacle is Obstacle.VALUE_NEEDED
    assert "AGENTMAIL_API_KEY" in ei.value.prompt
    assert driver.calls == []


@pytest.mark.asyncio
async def test_no_inbox_client_refuses_before_any_page():
    class NoClientMail:
        client = None

        async def wait_for_code(self, timeout=120):
            return None
    driver = FakeDriver(probes=["ok", "ok", "ok"])
    with pytest.raises(SignupPaused) as ei:
        await _flow(driver, mail=NoClientMail()).run()
    assert ei.value.obstacle is Obstacle.VALUE_NEEDED
    assert driver.calls == []


@pytest.mark.asyncio
async def test_profile_outcome_is_reported_not_assumed():
    """The driver says what it applied; the result carries it verbatim."""
    class Driver(FakeDriver):
        async def set_handle_and_profile(self, handle, bio):
            self.bio = bio
            return {"handle_applied": False, "bio_applied": True, "handle": "robbot"}
    result = await _flow(Driver(probes=["ok", "ok", "ok"])).run()
    assert result.requested_handle == "robbot"
    assert result.handle_applied is False and result.bio_applied is True
