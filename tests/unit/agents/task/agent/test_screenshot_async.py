"""B-T5 — async screenshot save off the step path.

The blocking PIL encode+write used to run inline in the synchronous
``_make_history_item``, stalling the event loop. B-T5 makes ``_make_history_item``
async and offloads the write via ``save_screenshot_async`` (asyncio.to_thread).
"""
import inspect

import pytest

from agents.task.agent.core.history_io import HistoryIOMixin


class _Host(HistoryIOMixin):
    pass


def test_make_history_item_is_async():
    assert inspect.iscoroutinefunction(HistoryIOMixin._make_history_item)


def test_save_screenshot_async_is_coroutine():
    assert inspect.iscoroutinefunction(HistoryIOMixin.save_screenshot_async)


@pytest.mark.asyncio
async def test_save_screenshot_async_empty_returns_none():
    assert await _Host().save_screenshot_async("s1", "", step_number=0) is None


@pytest.mark.asyncio
async def test_save_screenshot_async_offloads_to_sync_writer():
    host = _Host()
    calls = {}

    def fake_sync(session_id, data, step_number=0, is_fullpage=False):
        calls["args"] = (session_id, data, step_number, is_fullpage)
        return "/tmp/shot.jpg"

    host.save_screenshot = fake_sync  # instance attr shadows the class method
    result = await host.save_screenshot_async("s1", "DATA", 3, True)

    assert result == "/tmp/shot.jpg"
    assert calls["args"] == ("s1", "DATA", 3, True)
