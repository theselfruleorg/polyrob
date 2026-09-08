import pytest
from core.surfaces.dead_targets import DeadTargetStore
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface


class _RecordingSurface(Surface):
    def __init__(self, streaming=True):
        super().__init__()
        self._streaming = streaming
        self.sent = []
        self.streamed = []

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=self._streaming)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=True)

    async def start(self, container):
        pass

    async def stop(self):
        pass

    async def stream(self, msg):
        self.streamed.append(msg)


@pytest.fixture
def router(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    reg.bind("k1", "sess_1", "u_abc", "telegram", "555")
    return MessageRouter(reg), reg


@pytest.mark.asyncio
async def test_publish_routes_discrete_to_send(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="k1", text="done", partial=False))
    assert len(surf.sent) == 1 and surf.sent[0].text == "done"


@pytest.mark.asyncio
async def test_publish_routes_partial_to_stream(router):
    r, _ = router
    surf = _RecordingSurface(streaming=True)
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="k1", text="Hel", partial=True, stream_id="s1"))
    assert len(surf.streamed) == 1


@pytest.mark.asyncio
async def test_publish_scrubs_brain_state(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    leak = 'Here is the answer. {"current_state": {"next_goal": "x"}, "memory": "y"}'
    await r.publish(OutboundMessage(session_key="k1", text=leak, partial=False))
    assert "current_state" not in surf.sent[0].text
    assert "Here is the answer." in surf.sent[0].text


@pytest.mark.asyncio
async def test_publish_unknown_key_is_failopen_noop(router):
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.publish(OutboundMessage(session_key="UNKNOWN", text="x", partial=False))
    assert surf.sent == []  # no crash, nothing delivered


@pytest.mark.asyncio
async def test_publish_raising_surface_is_failopen(router):
    r, _ = router

    class _Boom(_RecordingSurface):
        async def send(self, msg):
            raise RuntimeError("surface down")

    r.subscribe("telegram", _Boom())
    # Must not raise
    await r.publish(OutboundMessage(session_key="k1", text="x", partial=False))


@pytest.mark.asyncio
async def test_send_message_backcompat_shim(router):
    r, reg = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    await r.send_message(chat_id="555", text="cron note", surface_id="telegram")
    assert len(surf.sent) == 1 and surf.sent[0].text == "cron note"
    assert surf.sent[0].media == []  # default keeps today's shape


@pytest.mark.asyncio
async def test_send_message_threads_media(router):
    r, reg = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    media = [{"kind": "image", "path": "/tmp/x.png", "caption": None}]
    await r.send_message(chat_id="555", text="see attached", surface_id="telegram", media=media)
    assert surf.sent[0].media == media


def test_capabilities_returns_subscribed_surface_caps(router):
    r, reg = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    caps = r.capabilities("telegram")
    assert caps is not None and caps.supports_streaming is True


def test_capabilities_returns_none_for_unknown_surface(router):
    r, reg = router
    assert r.capabilities("nope") is None


def test_bot_username_returns_subscribed_surface_handle(router):
    r, reg = router
    surf = _RecordingSurface()
    surf.bot_username = "tmachinroBot"
    r.subscribe("telegram", surf)
    assert r.bot_username("telegram") == "tmachinroBot"


def test_bot_username_returns_none_for_unknown_surface(router):
    r, reg = router
    assert r.bot_username("nope") is None


def test_bot_username_returns_none_when_surface_lacks_attribute(router):
    r, reg = router
    r.subscribe("telegram", _RecordingSurface())
    assert r.bot_username("telegram") is None


# ---------------------------------------------------------------------------
# T1.5: dead-target gate on the direct-send path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_skips_send_for_dead_target(router, tmp_path):
    """A target already marked dead is never handed to the surface (direct path)."""
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    store.mark("telegram", "555", "blocked")  # chat_id from the router fixture's binding
    r.attach_dead_targets(store)
    await r.publish(OutboundMessage(session_key="k1", text="hello", partial=False))
    assert surf.sent == []
    assert surf.streamed == []


@pytest.mark.asyncio
async def test_publish_marks_target_dead_on_forbidden_failure(router, tmp_path):
    """A direct send that fails with a classifiable ('Forbidden: bot was blocked...')
    error marks the target dead — same classify+mark contract as the dispatcher."""
    r, _ = router

    class _Forbidden(_RecordingSurface):
        async def send(self, msg):
            self.sent.append(msg)
            return SendResult(success=False, error="Forbidden: bot was blocked by the user")

    surf = _Forbidden()
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    await r.publish(OutboundMessage(session_key="k1", text="hi", partial=False))
    assert len(surf.sent) == 1          # the send was actually attempted (not pre-marked)
    assert store.is_dead("telegram", "555") is True


@pytest.mark.asyncio
async def test_publish_transient_failure_not_marked_dead(router, tmp_path):
    """An unclassified (transient) failure must NOT mark the target dead."""
    r, _ = router

    class _Flaky(_RecordingSurface):
        async def send(self, msg):
            self.sent.append(msg)
            return SendResult(success=False, error="timeout contacting provider")

    surf = _Flaky()
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    await r.publish(OutboundMessage(session_key="k1", text="hi", partial=False))
    assert store.is_dead("telegram", "555") is False


@pytest.mark.asyncio
async def test_publish_dead_target_gate_fail_open_on_corrupted_store(router, tmp_path):
    """A store that fails to read (corrupted db) must never block a send — mirrors
    DeadTargetStore's own fail-open contract (test_dead_targets.py)."""
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    db = str(tmp_path / "dead.db")
    store = DeadTargetStore(db)
    r.attach_dead_targets(store)
    with open(db, "wb") as f:
        f.write(b"not a sqlite database at all" * 50)
    await r.publish(OutboundMessage(session_key="k1", text="still works", partial=False))
    assert len(surf.sent) == 1


# ---------------------------------------------------------------------------
# Final-review fixes (T1.5): send_message gate + result-blindness, mark misattribution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_skips_dead_target_without_calling_surface(router, tmp_path):
    """A provably-dead target is skipped by send_message before the surface is ever
    invoked (mirrors the publish() direct-path gate)."""
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    store.mark("telegram", "555", "blocked")
    r.attach_dead_targets(store)
    result = await r.send_message(chat_id="555", text="hi", surface_id="telegram")
    assert result is False
    assert surf.sent == []


@pytest.mark.asyncio
async def test_send_message_marks_dead_and_returns_false_on_forbidden(router, tmp_path):
    """A classifiable send failure (e.g. 'Forbidden: bot was blocked...') marks the
    target dead AND returns False — a proactive send failure must be visible to the
    caller, not swallowed as a silent True."""
    r, _ = router

    class _Forbidden(_RecordingSurface):
        async def send(self, msg):
            self.sent.append(msg)
            return SendResult(success=False, error="Forbidden: bot was blocked by the user")

    surf = _Forbidden()
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    result = await r.send_message(chat_id="555", text="hi", surface_id="telegram")
    assert result is False
    assert len(surf.sent) == 1  # send was attempted (not pre-marked)
    assert store.is_dead("telegram", "555") is True


@pytest.mark.asyncio
async def test_send_message_returns_true_on_successful_send_result(router, tmp_path):
    """A successful SendResult still returns True, unchanged (with the dead-target
    store attached — the gate must not regress the happy path)."""
    r, _ = router
    surf = _RecordingSurface()  # returns SendResult(success=True)
    r.subscribe("telegram", surf)
    store = DeadTargetStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    result = await r.send_message(chat_id="555", text="hi", surface_id="telegram")
    assert result is True
    assert len(surf.sent) == 1


@pytest.mark.asyncio
async def test_send_message_falls_back_to_queue_when_surface_not_local(router, tmp_path, monkeypatch):
    """2026-08-28: `message(surface="email")` from the telegram daemon process used to
    be a guaranteed False — "email" is only subscribed in polyrob-email.service's OWN
    MessageRouter instance. When a durable queue is attached, an unrecognized surface
    now enqueues for cross-process delivery (drained by whichever process DOES host
    that surface) instead of failing immediately."""
    from core.surfaces.outbound_queue import OutboundDeliveryQueue
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, _ = router
    q = OutboundDeliveryQueue(str(tmp_path / "outbox.db"))
    r.attach_queue(q)
    # no "email" surface subscribed at all
    result = await r.send_message(chat_id="rob@example.com", text="hi", surface_id="email")
    assert result is True
    rows = q.claim_due(now=__import__("time").time() + 1)
    assert len(rows) == 1
    assert rows[0]["surface_id"] == "email"
    assert rows[0]["dest"] == "rob@example.com"
    assert rows[0]["payload"] == "hi"
    assert rows[0]["session_key"] == "direct:email:rob@example.com"


@pytest.mark.asyncio
async def test_send_message_queue_fallback_threads_media(router, tmp_path, monkeypatch):
    from core.surfaces.outbound_queue import OutboundDeliveryQueue
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, _ = router
    q = OutboundDeliveryQueue(str(tmp_path / "outbox.db"))
    r.attach_queue(q)
    media = [{"kind": "document", "path": "/tmp/report.pdf", "caption": None}]
    result = await r.send_message(chat_id="rob@example.com", text="see attached",
                                  surface_id="email", media=media)
    assert result is True
    rows = q.claim_due(now=__import__("time").time() + 1)
    assert len(rows) == 1
    import json
    assert json.loads(rows[0]["media"]) == media


@pytest.mark.asyncio
async def test_send_message_no_queue_still_returns_false_for_unknown_surface(router):
    """Byte-identical legacy behavior when no queue is attached at all (the
    OUTBOUND_QUEUE_ENABLED=false / not-yet-installed default)."""
    r, _ = router
    result = await r.send_message(chat_id="rob@example.com", text="hi", surface_id="email")
    assert result is False


@pytest.mark.asyncio
async def test_send_message_fallback_ignores_flag_uses_attached_queue(router, tmp_path, monkeypatch):
    """2026-08-30: the cross-process fallback is keyed on queue EXISTENCE only, not
    OUTBOUND_QUEUE_ENABLED — that flag governs ONLY publish()'s primary reply-routing
    decision (bootstrap.py now constructs the queue unconditionally). With a queue
    attached, this fallback works regardless of the flag's setting, closing the gap
    where `message(surface="email")` from an autonomous goal permanently failed on a
    prod deploy that never turned the flag on (observed 2026-08-28/2026-08-30)."""
    from core.surfaces.outbound_queue import OutboundDeliveryQueue
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "false")
    r, _ = router
    q = OutboundDeliveryQueue(str(tmp_path / "outbox.db"))
    r.attach_queue(q)
    result = await r.send_message(chat_id="rob@example.com", text="hi", surface_id="email")
    assert result is True
    rows = q.claim_due(now=__import__("time").time() + 1)
    assert len(rows) == 1
    assert rows[0]["surface_id"] == "email"


@pytest.mark.asyncio
async def test_send_message_known_surface_never_touches_queue(router, tmp_path, monkeypatch):
    """A locally-subscribed surface always sends directly — the queue fallback is
    only reached when the surface lookup itself misses."""
    from core.surfaces.outbound_queue import OutboundDeliveryQueue
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    r, _ = router
    surf = _RecordingSurface()
    r.subscribe("telegram", surf)
    q = OutboundDeliveryQueue(str(tmp_path / "outbox.db"))
    r.attach_queue(q)
    result = await r.send_message(chat_id="555", text="hi", surface_id="telegram")
    assert result is True
    assert len(surf.sent) == 1
    assert q.claim_due(now=__import__("time").time() + 1) == []


@pytest.mark.asyncio
async def test_send_message_mark_fault_does_not_flip_returned_result(router, tmp_path):
    """A dead-target STORE fault while marking a failed send must not change the
    (already-False) result returned to the caller — the send outcome is still
    reported honestly even though telemetry-adjacent bookkeeping broke."""
    r, _ = router

    class _Forbidden(_RecordingSurface):
        async def send(self, msg):
            self.sent.append(msg)
            return SendResult(success=False, error="Forbidden: bot was blocked by the user")

    class _BoomStore(DeadTargetStore):
        def mark(self, surface, address, reason):
            raise RuntimeError("disk full")

    surf = _Forbidden()
    r.subscribe("telegram", surf)
    store = _BoomStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    result = await r.send_message(chat_id="555", text="hi", surface_id="telegram")
    assert result is False


@pytest.mark.asyncio
async def test_publish_mark_fault_not_misattributed_to_surface(router, tmp_path, caplog):
    """Fix 3: a dead-target STORE fault while marking a failed send in publish()'s
    direct-send path must be logged as a mark failure, NEVER folded into the
    generic 'surface ... raised' fail-open log (misattribution) — the surface itself
    did not raise; its SendResult just carried success=False."""
    import logging
    r, _ = router

    class _Forbidden(_RecordingSurface):
        async def send(self, msg):
            self.sent.append(msg)
            return SendResult(success=False, error="Forbidden: bot was blocked by the user")

    class _BoomStore(DeadTargetStore):
        def mark(self, surface, address, reason):
            raise RuntimeError("disk full")

    surf = _Forbidden()
    r.subscribe("telegram", surf)
    store = _BoomStore(str(tmp_path / "dead.db"))
    r.attach_dead_targets(store)
    with caplog.at_level(logging.ERROR):
        await r.publish(OutboundMessage(session_key="k1", text="hi", partial=False))
    assert len(surf.sent) == 1
    assert not any("surface" in rec.getMessage() and "raised" in rec.getMessage()
                   for rec in caplog.records)
    assert any("dead-target mark failed" in rec.getMessage() for rec in caplog.records)
