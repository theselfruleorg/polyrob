import os
import pytest
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.outbound_dispatcher import OutboundDispatcher
from core.surfaces.envelopes import SendResult
from core.surfaces.dead_targets import DeadTargetStore


class _Surface:
    def __init__(self, results): self._results = list(results); self.sent = []

    @property
    def surface_id(self): return "wa"

    async def send(self, msg):
        self.sent.append(msg.text)
        r = self._results.pop(0)
        if isinstance(r, Exception): raise r
        return r


class _EvLog:
    """In-memory stand-in for TelemetryEventLog (record subset). Mirrors
    ``tests/unit/core/surfaces/test_user_delivery.py::_EvLog`` — the dispatcher
    takes this via dependency injection (``event_log=``) rather than importing
    the agents-tier telemetry module itself."""

    def __init__(self): self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None,
               attrs=None, **kw):
        merged = dict(kw)
        if attrs:
            merged.update(attrs)
        self.events.append({"kind": kind, "user_id": user_id,
                            "session_id": session_id, "source": source,
                            "attrs": merged})


@pytest.mark.asyncio
async def test_delivers_and_marks_delivered(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=100.0)
    assert n == 1 and surf.sent == ["hi"]
    assert q.counts()["delivered"] == 1


@pytest.mark.asyncio
async def test_failure_reschedules_then_dead_letters(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=False, error="429")] * 2)
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=2, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    await d.drain_once(now=0.0)
    assert q.counts()["pending"] == 1          # rescheduled, not delivered
    # jump past backoff; second failure hits max_attempts -> dead
    await d.drain_once(now=10_000.0)
    assert q.counts()["dead"] == 1


@pytest.mark.asyncio
async def test_raising_surface_reschedules_not_crash(tmp_path):
    """A surface that raises must not crash the worker — fail-open."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([RuntimeError("network down")])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=3, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=0.0)
    assert n == 0
    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["dead"] == 0


@pytest.mark.asyncio
async def test_unknown_surface_reschedules(tmp_path):
    """surface_lookup returning None = reschedule with 'no surface' error."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="missing", dest="1", payload="hi")
    d = OutboundDispatcher(q, lambda sid: None, max_attempts=3, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)
    n = await d.drain_once(now=0.0)
    assert n == 0
    assert q.counts()["pending"] == 1


@pytest.mark.asyncio
async def test_rate_limited_row_reschedules_not_delivered(tmp_path):
    """Token bucket exhausted → row put back to pending with future next_attempt_at."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    # burst=0 means no tokens available from the start
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1.0, burst=0)
    n = await d.drain_once(now=0.0)
    assert n == 0
    assert surf.sent == []          # never sent
    assert q.counts()["pending"] == 1


@pytest.mark.asyncio
async def test_stop_sets_event(tmp_path):
    """stop() sets the internal event so run() exits."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    d = OutboundDispatcher(q, lambda sid: None)
    assert not d._stop.is_set()
    await d.stop()
    assert d._stop.is_set()


# ---------------------------------------------------------------------------
# Dead-target registry gate + mark (T1.5, Task 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_marked_target_is_skipped_and_dead_lettered(tmp_path):
    """A target already marked dead is skipped entirely: never sent, dead-lettered
    immediately (never rescheduled)."""
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    dt.mark("wa", "1", "blocked")

    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])   # would succeed if ever called
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000, dead_targets=dt)

    n = await d.drain_once(now=100.0)

    assert n == 0
    assert surf.sent == []                 # surface never invoked
    counts = q.counts()
    assert counts["dead"] == 1
    assert counts["pending"] == 0


@pytest.mark.asyncio
async def test_pre_marked_target_skip_emits_event(tmp_path):
    """The skip records a dead_target_skipped telemetry event via the injected
    ``event_log`` (no direct import of the telemetry module by the dispatcher)."""
    ev = _EvLog()
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    dt.mark("wa", "1", "blocked")

    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000,
                           dead_targets=dt, event_log=ev)

    await d.drain_once(now=0.0)

    assert len(ev.events) == 1
    event = ev.events[0]
    assert event["kind"] == "dead_target_skipped"
    assert event["attrs"]["surface"] == "wa"
    assert event["attrs"]["address"] == "1"
    assert event["attrs"]["reason"] == "registry"


@pytest.mark.asyncio
async def test_forbidden_failure_marks_dead_and_dead_letters(tmp_path):
    """A Telegram 'bot was blocked' failure is a definitive liveness signal: mark
    dead + dead-letter immediately, never reschedule (even on the very first
    attempt, well under max_attempts). Also emits dead_target_marked via the
    injected event log."""
    ev = _EvLog()
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=False, error="Forbidden: bot was blocked by the user")])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=6, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000, dead_targets=dt, event_log=ev)

    await d.drain_once(now=0.0)

    assert dt.is_dead("wa", "1") is True
    counts = q.counts()
    assert counts["dead"] == 1
    assert counts["pending"] == 0
    assert len(ev.events) == 1
    event = ev.events[0]
    assert event["kind"] == "dead_target_marked"
    assert event["attrs"]["surface"] == "wa"
    assert event["attrs"]["reason"] == "blocked"


@pytest.mark.asyncio
async def test_timeout_failure_not_marked_reschedules_as_today(tmp_path):
    """A transient/unknown failure (e.g. 'timeout') is NOT a liveness signal:
    behavior is byte-identical to legacy — rescheduled, not marked, not dead."""
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=False, error="timeout")])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=6, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000, dead_targets=dt)

    await d.drain_once(now=0.0)

    assert dt.is_dead("wa", "1") is False
    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["dead"] == 0


@pytest.mark.asyncio
async def test_flag_off_disables_gate_and_mark(tmp_path, monkeypatch):
    """DEAD_TARGET_REGISTRY=false: a pre-marked target is NOT skipped, and a
    classifiable failure does NOT get newly marked — legacy bytes end to end."""
    monkeypatch.setenv("DEAD_TARGET_REGISTRY", "false")
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    dt.mark("wa", "premarked", "blocked")

    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="premarked", payload="hi")
    q.enqueue(idempotency_key="b", session_key="s", surface_id="wa", dest="fresh", payload="bye")
    surf = _Surface([
        SendResult(success=True),
        SendResult(success=False, error="Forbidden: bot was blocked by the user"),
    ])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=6, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000, dead_targets=dt)

    await d.drain_once(now=0.0)

    assert surf.sent == ["hi", "bye"]        # gate never engaged; both rows sent
    counts = q.counts()
    assert counts["delivered"] == 1          # premarked row delivered despite dead mark
    assert counts["pending"] == 1            # fresh row rescheduled (mark step skipped)
    assert dt.is_dead("wa", "fresh") is False


@pytest.mark.asyncio
async def test_no_dead_targets_store_is_byte_identical_legacy(tmp_path):
    """dead_targets=None (the default): no gate, no mark — indistinguishable from
    the dispatcher before this feature existed."""
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=False, error="Forbidden: bot was blocked by the user")])
    d = OutboundDispatcher(q, lambda sid: surf, max_attempts=6, base_backoff=1.0,
                           rate_per_sec=1000, burst=1000)   # no dead_targets kwarg

    await d.drain_once(now=0.0)

    assert surf.sent == ["hi"]
    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["dead"] == 0


@pytest.mark.asyncio
async def test_attach_event_log_wires_post_construction(tmp_path):
    """Fix 1 (final review): a dispatcher built with event_log=None (bootstrap's
    real-world shape — core must never import agents.*) still emits
    dead_target_skipped once a caller (e.g. the CLI dispatcher-start seam) attaches
    an event log post-construction via attach_event_log()."""
    ev = _EvLog()
    dt = DeadTargetStore(os.path.join(tmp_path, "dt.db"))
    dt.mark("wa", "1", "blocked")

    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="s", surface_id="wa", dest="1", payload="hi")
    surf = _Surface([SendResult(success=True)])
    d = OutboundDispatcher(q, lambda sid: surf, rate_per_sec=1000, burst=1000,
                           dead_targets=dt)   # no event_log kwarg — mirrors bootstrap

    d.attach_event_log(ev)
    await d.drain_once(now=0.0)

    assert len(ev.events) == 1
    event = ev.events[0]
    assert event["kind"] == "dead_target_skipped"
    assert event["attrs"]["surface"] == "wa"
