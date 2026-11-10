import asyncio

import pytest

from surfaces.telegram.poll_batch import WORKERS, dispatch_batch


def update(chat, number):
    return {"update_id": number, "message": {"chat": {"id": chat}}}


@pytest.mark.asyncio
async def test_slow_voice_does_not_block_another_chat_and_same_chat_stays_ordered():
    release = asyncio.Event()
    other = asyncio.Event()
    seen = []

    async def handle(row):
        number = row["update_id"]
        seen.append(number)
        if number == 1:
            await release.wait()
        if number == 3:
            other.set()

    task = asyncio.create_task(dispatch_batch(
        [update(1, 1), update(1, 2), update(2, 3)], handle))
    try:
        await asyncio.wait_for(other.wait(), 1)
        assert seen == [1, 3]
    finally:
        release.set()
        await task
    assert seen == [1, 3, 2]


@pytest.mark.asyncio
async def test_bounded_workers_are_cancelled_with_the_poll():
    active = 0
    peak = 0
    filled = asyncio.Event()

    async def handle(row):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == WORKERS:
            filled.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    task = asyncio.create_task(dispatch_batch([update(n, n) for n in range(100)], handle))
    await asyncio.wait_for(filled.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and peak == WORKERS


@pytest.mark.asyncio
async def test_callback_and_message_in_same_chat_are_ordered():
    seen = []

    async def handle(row):
        await asyncio.sleep(0)
        seen.append(row["update_id"])

    await dispatch_batch([
        update(1, 1),
        {"update_id": 2, "callback_query": {"message": {"chat": {"id": 1}}}},
        update(1, 3),
    ], handle)
    assert seen == [1, 2, 3]


def large(chat, number):
    return {"update_id": number, "message": {
        "chat": {"id": chat}, "video": {"file_id": "v", "file_size": 6 * 1024 * 1024}}}


@pytest.mark.asyncio
async def test_a_large_download_does_not_hold_the_batch_and_its_chat_stays_ordered():
    from surfaces.telegram.poll_batch import BackgroundLanes

    release = asyncio.Event()
    seen = []

    async def handle(row):
        if row["update_id"] == 1:
            await release.wait()
        seen.append(row["update_id"])

    lanes = BackgroundLanes()
    await dispatch_batch([large(1, 1), update(2, 2)], handle, lanes)
    assert seen == [2]
    await dispatch_batch([update(1, 3), update(2, 4)], handle, lanes)
    assert seen == [2, 4]
    release.set()
    await asyncio.sleep(0.01)
    assert seen == [2, 4, 1, 3]
    assert not lanes.holds("1")


@pytest.mark.asyncio
async def test_a_full_set_of_lanes_runs_the_next_large_chat_in_the_batch():
    from surfaces.telegram.poll_batch import MAX_BACKGROUND_CHATS, BackgroundLanes

    release = asyncio.Event()
    seen = []

    async def handle(row):
        if row["update_id"] < MAX_BACKGROUND_CHATS:
            await release.wait()
        seen.append(row["update_id"])

    lanes = BackgroundLanes()
    await dispatch_batch([large(n, n) for n in range(MAX_BACKGROUND_CHATS)], handle, lanes)
    await dispatch_batch([large(99, 99)], handle, lanes)
    assert seen == [99]
    await lanes.close()


@pytest.mark.asyncio
async def test_close_cancels_a_lane_and_logs_the_lost_chat(monkeypatch):
    # The logger itself: an earlier test in a full run can leave it out of caplog's reach.
    from unittest.mock import MagicMock
    from surfaces.telegram import poll_batch
    from surfaces.telegram.poll_batch import BackgroundLanes

    log = MagicMock()
    monkeypatch.setattr(poll_batch, "logger", log)

    async def handle(row):
        await asyncio.Event().wait()

    lanes = BackgroundLanes()
    await dispatch_batch([large(7, 1)], handle, lanes)
    await asyncio.sleep(0)
    await lanes.close()
    assert "7" in str(log.warning.call_args.args[1])


@pytest.mark.asyncio
async def test_a_chat_that_keeps_writing_behind_its_lane_holds_one_task():
    """Each batch of a held chat used to add a task that waited on the last one: a
    chain with no bound. Its rows now wait in the one running lane, in order."""
    from surfaces.telegram.poll_batch import BackgroundLanes

    release = asyncio.Event()
    seen = []

    async def handle(row):
        if row["update_id"] == 1:
            await release.wait()
        seen.append(row["update_id"])

    lanes = BackgroundLanes()
    await dispatch_batch([large(1, 1)], handle, lanes)
    first = lanes._tasks["1"]
    for n in range(2, 12):
        await dispatch_batch([update(1, n)], handle, lanes)
    assert lanes._tasks["1"] is first and len(asyncio.all_tasks()) < 5
    release.set()
    await asyncio.sleep(0.01)
    assert seen == list(range(1, 12)) and not lanes.holds("1")
