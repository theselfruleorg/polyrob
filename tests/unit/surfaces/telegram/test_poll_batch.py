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
