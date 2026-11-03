"""098 AG-12: the after-run reviewer's in-flight key is released by the task's end."""
import asyncio

import pytest

import agents.task.agent.core.background_review as br


@pytest.mark.asyncio
async def test_in_flight_key_released_when_cancelled_before_start(monkeypatch):
    monkeypatch.setattr(br, "review_after_run_due", lambda regime, meta: True)
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
