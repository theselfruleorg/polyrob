"""Shell-test hygiene: stop the 073 W4 job watchers a test left running, inside
the test's own event loop, so a closed loop never destroys a pending watcher."""
import asyncio

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _stop_job_watchers():
    yield
    try:
        from tools.shell.watch import stop_watches
        tasks = stop_watches()
    except Exception:
        return
    mine = []
    loop = asyncio.get_running_loop()
    for t in tasks:
        try:
            if t.get_loop() is loop:
                mine.append(t)
        except Exception:
            pass
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)
