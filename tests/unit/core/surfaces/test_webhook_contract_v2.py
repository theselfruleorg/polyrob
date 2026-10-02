"""Webhook contract v2 (064 S2b F4).

- a surface answers a JSON challenge and a text challenge (raw, verbatim);
- persist-before-ack: a crash between the ack and the turn replays the event
  exactly once after a restart;
- the body cap runs before the signature check; the signature before the parse;
- reply windows are data, and WhatsApp's 24 h window reads byte-equal;
- the bot-pair loop guard drops a looping bot pair, never a human.
"""
import hashlib
import asyncio
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.surfaces.envelopes import (
    Identity, InboundMessage, ReplyWindow, SessionSource, SurfaceCapabilities,
)
from core.surfaces.idempotency import AcceptedEventJournal, IdempotencyStore
from core.surfaces.inbound_webhook import WebhookResponse, WebhookSurface

SECRET = b"s3cret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


class _FakeHook(WebhookSurface):
    """A Feishu-shaped test surface: HMAC header, a JSON url_verification echo in
    the POST body, a text challenge on GET, and 5-second-ack journaling."""

    ack_before_turn = True
    parsed = 0

    @property
    def surface_id(self):
        return "fakehook"

    def verify_signature(self, headers, body):
        sig = headers.get("x-sig", "")
        return bool(sig) and hmac.compare_digest(sig, _sign(body))

    def verify_challenge(self, params):
        if params.get("echostr"):
            return WebhookResponse.text(params["echostr"])
        return None

    def answer_post(self, payload):
        if payload.get("type") == "url_verification":
            return WebhookResponse.json({"challenge": payload["challenge"]})
        return None

    def parse(self, payload):
        type(self).parsed += 1
        return [InboundMessage(
            text=payload.get("text", ""),
            identity=Identity(user_id="u_fakehook_1",
                              source=SessionSource("fakehook", "c1"),
                              raw_user_id="1"),
            idempotency_key=payload.get("id"))]

    def idempotency_key(self, inbound):
        return f"fakehook:{inbound.idempotency_key}"


@pytest.fixture
def turns(monkeypatch):
    """Count agent turns: route + act are stubbed at the base class's imports."""
    ran = []

    async def _route(container, inbound):
        return object()

    async def _act(task_agent, result, **kw):
        ran.append(result.inbound.text)
        return None

    monkeypatch.setattr("core.surfaces.inbound_webhook.route_inbound", _route)
    monkeypatch.setattr("core.surfaces.inbound_webhook.act_on_inbound", _act)
    return ran


def _surface(tmp_path):
    db = str(tmp_path / "fakehook.db")
    return _FakeHook(IdempotencyStore(db), journal=AcceptedEventJournal(db))


def _client(surface):
    from api.webhooks import router, set_container_provider

    class _C:
        def get_service(self, name):
            return {"webhook_surfaces": {"fakehook": surface}}.get(name)

    set_container_provider(lambda: _C())
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_answers_a_json_challenge_verbatim(tmp_path, turns):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    res = _client(_surface(tmp_path)).post(
        "/webhooks/fakehook", content=body, headers={"x-sig": _sign(body)})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"challenge": "abc123"}
    assert turns == []


def test_answers_a_text_challenge_verbatim(tmp_path):
    res = _client(_surface(tmp_path)).get("/webhooks/fakehook?echostr=plain-777")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert res.text == "plain-777"


def test_challenge_needs_a_valid_signature(tmp_path, turns):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    res = _client(_surface(tmp_path)).post(
        "/webhooks/fakehook", content=body, headers={"x-sig": "forged"})
    assert res.status_code == 401
    assert "abc" not in res.text


def test_body_over_the_cap_is_refused_before_signature_or_parse(tmp_path, turns, monkeypatch):
    _FakeHook.parsed = 0
    s = _surface(tmp_path)
    checked = []
    monkeypatch.setattr(s, "verify_signature", lambda h, b: checked.append(1) or True)
    body = b'{"text": "' + b"x" * (64 * 1024) + b'"}'
    res = _client(s).post("/webhooks/fakehook", content=body, headers={"x-sig": "x"})
    assert res.status_code == 413
    assert checked == [] and _FakeHook.parsed == 0 and turns == []


@pytest.mark.asyncio
async def test_bad_signature_never_parses(tmp_path, turns):
    _FakeHook.parsed = 0
    s = _surface(tmp_path)
    out = await s.handle_post(None, {"x-sig": "nope"}, b'{"id": "1"}', None)
    assert out["ok"] is False
    assert _FakeHook.parsed == 0 and turns == []


@pytest.mark.asyncio
async def test_crash_between_ack_and_turn_replays_exactly_once(tmp_path, turns, monkeypatch):
    body = json.dumps({"id": "m1", "text": "hello"}).encode()
    s1 = _surface(tmp_path)
    # The process dies right after the ack: the drain never runs.
    monkeypatch.setattr(s1, "_spawn_drain", lambda *a, **k: None)
    out = await s1.handle_post(None, {"x-sig": _sign(body)}, body, None)
    assert out == {"ok": True}
    assert turns == []                               # acked, no turn yet

    s2 = _surface(tmp_path)                          # the restart
    assert await s2.recover(None, None) == 1
    assert turns == ["hello"]
    assert await s2.recover(None, None) == 0         # once
    # The platform redelivers the same event after the restart: no second turn.
    await s2.handle_post(None, {"x-sig": _sign(body)}, body, None)
    for t in list(s2._tasks):
        await t
    assert turns == ["hello"]


@pytest.mark.asyncio
async def test_ack_returns_before_the_turn_and_the_drain_runs_it(tmp_path, turns):
    body = json.dumps({"id": "m2", "text": "later"}).encode()
    s = _surface(tmp_path)
    out = await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    assert out == {"ok": True}
    for t in list(s._tasks):
        await t
    assert turns == ["later"]
    assert s._journal.pending("fakehook") == []


@pytest.mark.asyncio
async def test_a_processing_row_left_by_a_dead_drain_is_recovered(tmp_path, turns):
    body = json.dumps({"id": "m3", "text": "stuck"}).encode()
    j = AcceptedEventJournal(str(tmp_path / "fakehook.db"))
    j.accept("k3", "fakehook", body)
    assert j.claim("k3") == body                     # a drain held it, then died
    s = _surface(tmp_path)
    assert await s.recover(None, None) == 1
    assert turns == ["stuck"]


@pytest.mark.asyncio
async def test_failed_turn_is_retained_and_redelivery_retries(tmp_path, turns, monkeypatch):
    import core.surfaces.inbound_webhook as hooks

    body = json.dumps({"id": "retry", "text": "keep me"}).encode()
    s = _surface(tmp_path)
    original = hooks.act_on_inbound

    async def unavailable(*args, **kwargs):
        raise RuntimeError("agent temporarily unavailable")

    monkeypatch.setattr(hooks, "act_on_inbound", unavailable)
    await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    for task in list(s._tasks):
        await task
    assert s._journal.pending(s.surface_id)
    assert not s._idem.peek("fakehook:retry")
    monkeypatch.setattr(hooks, "act_on_inbound", original)
    await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    for task in list(s._tasks):
        await task
    assert turns == ["keep me"]
    assert s._journal.pending(s.surface_id) == []


@pytest.mark.asyncio
async def test_canceled_turn_recovers_without_a_stale_dedup_claim(tmp_path, turns, monkeypatch):
    import core.surfaces.inbound_webhook as hooks

    s = _surface(tmp_path)
    body = json.dumps({"id": "canceled", "text": "recover me"}).encode()
    started = asyncio.Event()
    original = hooks.act_on_inbound

    async def interrupted(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(hooks, "act_on_inbound", interrupted)
    await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    await asyncio.wait_for(started.wait(), 2)
    task = next(iter(s._tasks))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(hooks, "act_on_inbound", original)
    assert await _surface(tmp_path).recover(None, None) == 1
    assert turns == ["recover me"]


@pytest.mark.asyncio
async def test_concurrent_envelopes_for_one_message_run_once(tmp_path, turns):
    s = _surface(tmp_path)
    # Platforms may wrap one message in different retry envelopes.
    for retry in [0, 1]:
        body = json.dumps({"id": "same", "text": "once", "retry": retry}).encode()
        await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    for task in list(s._tasks):
        await task
    assert turns == ["once"]


@pytest.mark.asyncio
async def test_batch_retry_skips_completed_messages(tmp_path, turns, monkeypatch):
    import core.surfaces.inbound_webhook as hooks

    s = _surface(tmp_path)
    parse = s.parse
    monkeypatch.setattr(s, "parse", lambda payload: [parse(row)[0] for row in payload["messages"]])
    original = hooks.act_on_inbound
    fail = True

    async def partial_failure(task_agent, result, **kw):
        if fail and result.inbound.text == "second":
            raise RuntimeError("temporary failure")
        return await original(task_agent, result, **kw)

    monkeypatch.setattr(hooks, "act_on_inbound", partial_failure)
    body = json.dumps({"messages": [
        {"id": "a", "text": "first"}, {"id": "b", "text": "second"},
    ]}).encode()
    await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
    for task in list(s._tasks):
        await task
    assert turns == ["first"]
    fail = False
    assert await s.recover(None, None) == 1
    assert turns == ["first", "second"]


@pytest.mark.asyncio
async def test_exhausted_failure_stays_for_inspection(tmp_path, turns, monkeypatch):
    s = _surface(tmp_path)

    def bad_parse(payload):
        raise ValueError("cannot parse this event")

    monkeypatch.setattr(s, "parse", bad_parse)
    body = b'{"id":"bad"}'
    for _ in range(s._journal.max_attempts + 1):
        await s.handle_post(None, {"x-sig": _sign(body)}, body, None)
        for task in list(s._tasks):
            await task
    from core.sqlite_util import execute_retry
    row = execute_retry(s._journal.db_path, "SELECT state, attempts FROM accepted_events", fetch="one")
    assert row["state"] == "failed" and row["attempts"] == s._journal.max_attempts
    assert turns == []


# --- reply windows as data -----------------------------------------------------

def test_whatsapp_window_reads_byte_equal():
    from core.surfaces.send_policy import SendDecision
    from surfaces.whatsapp.surface import WhatsAppSurface

    class _Tracker:
        def __init__(self, last):
            self.last = last

        def last_inbound(self, phone):
            return self.last

    s = WhatsAppSurface(client=None)
    key = "agent:main:whatsapp:dm:15550001111"
    assert s.can_send_now(key, now=1000.0) == SendDecision.ALLOW        # no tracker
    s.attach_window(_Tracker(None))
    assert s.can_send_now(key, now=1000.0) == SendDecision.TEMPLATE_ONLY
    s.attach_window(_Tracker(1000.0))
    assert s.can_send_now(key, now=1000.0 + 86400) == SendDecision.ALLOW
    assert s.can_send_now(key, now=1000.0 + 86401) == SendDecision.TEMPLATE_ONLY
    w = s.capabilities.effective_reply_window()
    assert (w.kind, w.ttl_s, w.outside) == ("service_window", 86400, "template_only")


def test_a_token_window_denies_after_its_ttl():
    from core.surfaces.send_policy import SendDecision, window_decision
    w = ReplyWindow("token", 60, outside="deny")
    assert window_decision(w, 100.0, 150.0) == SendDecision.ALLOW
    assert window_decision(w, 100.0, 161.0) == SendDecision.DENY
    assert window_decision(None, None, 0.0) == SendDecision.ALLOW
    assert SurfaceCapabilities().effective_reply_window() is None


# --- bot-pair loop guard ----------------------------------------------------------

def _bot_line(peer="b1", bot=True):
    return InboundMessage(
        text="hi", sender_is_bot=bot,
        identity=Identity(user_id=f"u_discord_{peer}",
                          source=SessionSource("discord", "chan"), raw_user_id=peer))


@pytest.mark.asyncio
async def test_bot_pair_guard_drops_a_loop_then_cools_down(monkeypatch):
    from core.surfaces import bot_pair_guard, dispatcher
    bot_pair_guard.reset()
    for i in range(20):
        assert bot_pair_guard.allow("discord", "b1", now=1000.0 + i)
    assert bot_pair_guard.allow("discord", "b1", now=1020.0) is False   # 21st
    assert bot_pair_guard.allow("discord", "b1", now=1079.0) is False   # cooling
    assert bot_pair_guard.allow("discord", "b2", now=1021.0) is True    # other pair
    assert bot_pair_guard.allow("discord", "b1", now=1081.0) is True    # cooled

    bot_pair_guard.reset()
    monkeypatch.setattr(bot_pair_guard, "allow", lambda *a, **k: False)
    d = await dispatcher.route_inbound(None, _bot_line())
    assert d.kind == dispatcher.RouteKind.DENIED and d.reason == "bot_pair_guard"
    assert d.silent is True
    assert dispatcher._bot_pair_refusal(_bot_line(bot=False)) is None   # humans never


def test_bot_pair_maps_stay_bounded(monkeypatch):
    from core.surfaces import bot_pair_guard as g
    g.reset()
    monkeypatch.setattr(g, "_MAX_PAIRS", 50)
    for i in range(500):
        g.allow("telegram", f"bot{i}", now=1000.0 + i * 0.001)
    assert len(g._events) <= 51
    # idle pairs are swept once their window has passed
    g.allow("telegram", "late", now=5000.0)
    for i in range(g._SWEEP_EVERY):
        g.allow("telegram", "late", now=5000.0)
    assert set(g._events) == {("telegram", "late")}
