"""098 AG-12: the after-run reviewer's in-flight key is released by the task's end."""
import asyncio

import pytest

import agents.task.agent.core.background_review as br


@pytest.mark.asyncio
async def test_in_flight_key_released_when_cancelled_before_start(monkeypatch):
    monkeypatch.setattr(br, "review_after_run_due", lambda regime, meta, outcome="": True)
    br._REVIEWS_IN_FLIGHT.discard("u1")
    br.schedule_review_after_run(object(), user_id="u1", rail_id="cron:j", regime="x")
    assert "u1" in br._REVIEWS_IN_FLIGHT
    from core.async_bridge import _RETAINED
    for t in list(_RETAINED):
        t.cancel()                        # e.g. the runtime stops before the task runs
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "u1" not in br._REVIEWS_IN_FLIGHT


@pytest.mark.asyncio
async def test_inline_review_not_spawned_twice_while_running():
    gate = asyncio.Event()
    runs = []

    class _A(br.BackgroundReviewMixin):
        orchestrator = None

        def _bg_review_should_fire(self, productive):
            return True

        async def _run_background_review(self):
            runs.append(1)
            await gate.wait()

    a = _A()
    a._maybe_spawn_background_review(turn_was_productive=True)
    await asyncio.sleep(0)
    a._maybe_spawn_background_review(turn_was_productive=True)
    await asyncio.sleep(0)
    assert runs == [1]
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    a._maybe_spawn_background_review(turn_was_productive=True)
    await asyncio.sleep(0)
    assert runs == [1, 1]
    gate.set()


@pytest.mark.asyncio
async def test_a_correction_review_waits_for_the_running_review_instead_of_being_lost(monkeypatch):
    """The verdict marks a run corrected once: a correction review skipped while another
    review ran was never made. It waits for that review and then runs."""
    gate = asyncio.Event()
    ran = []

    async def first(*_a, **_k):
        ran.append("run")
        await gate.wait()
        return True

    async def correction(*_a, **_k):
        ran.append("verdict")
        return True

    monkeypatch.setattr(br, "review_after_run_due", lambda regime, meta, outcome="": True)
    monkeypatch.setattr(br, "review_after_run", first)
    monkeypatch.setattr(br, "review_after_verdict", correction)
    br._REVIEWS_IN_FLIGHT.discard("u2")
    br.schedule_review_after_run(object(), user_id="u2", rail_id="cron:j", regime="x")
    await asyncio.sleep(0)
    episode = type("E", (), {"user_id": "u2"})()
    br.schedule_review_after_verdict(object(), None, episode=episode, rail_id="cron:j", meta={})
    await asyncio.sleep(0)
    assert ran == ["run"]
    gate.set()
    for _ in range(6):
        await asyncio.sleep(0)
    assert ran == ["run", "verdict"]
    assert "u2" not in br._REVIEWS_IN_FLIGHT and "u2" not in br._QUEUED_REVIEWS
