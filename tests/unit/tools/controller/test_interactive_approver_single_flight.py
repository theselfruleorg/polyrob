"""H8: InteractiveCLIApprover ran the blocking input() in a worker thread that a
wait_for timeout cannot interrupt. A second gated action would spawn a SECOND stdin
reader, and a late keystroke could be consumed by the wrong (stale) prompt. Only one
interactive prompt may own stdin at a time; a second concurrent prompt denies
(fail-closed) rather than racing for the operator's keystroke.
"""
import asyncio
import threading

import pytest

from tools.controller import approval_interactive
from tools.controller.approval_interactive import InteractiveCLIApprover


@pytest.fixture(autouse=True)
def _clear_stdin_flag():
    # The in-flight flag is a process-global; another test's timed-out prompt can leave
    # its (correctly) lingering worker thread holding it. Isolate each test here.
    approval_interactive._stdin_in_flight.clear()
    yield
    approval_interactive._stdin_in_flight.clear()


def test_approves_on_yes():
    a = InteractiveCLIApprover(input_fn=lambda p: "y")
    assert asyncio.run(a.request("act", {}, None)) is True


def test_denies_on_no():
    a = InteractiveCLIApprover(input_fn=lambda p: "no")
    assert asyncio.run(a.request("act", {}, None)) is False


def test_second_concurrent_prompt_denies_instead_of_racing_stdin():
    release = threading.Event()

    def blocking_input(prompt):
        release.wait(2)  # operator hasn't answered yet
        return "y"

    a = InteractiveCLIApprover(input_fn=blocking_input)

    async def scenario():
        t1 = asyncio.create_task(a.request("a1", {}, None))
        await asyncio.sleep(0.15)  # let t1 acquire stdin
        r2 = await a.request("a2", {}, None)  # while t1 still blocked
        release.set()
        r1 = await t1
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r2 is False   # second prompt denied — did not spawn a competing reader
    assert r1 is True    # first eventually approved
