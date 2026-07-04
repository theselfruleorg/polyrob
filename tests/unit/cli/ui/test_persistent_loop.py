"""D5: the persistent-input turn runner + scheduler (control flow, headless).

The actual Application.run_async() + run_in_terminal print routing needs a live
TTY (gated POLYROB_PERSISTENT_INPUT). What IS unit-testable is the control flow: one
turn runs start→respond→complete→end; a cancelled turn renders an interrupt; the
scheduler runs one turn at a time and interrupt cancels the in-flight task.
"""

from __future__ import annotations

import asyncio

from cli.ui.persistent_loop import TurnController, run_turn


class _FakeRenderer:
    def __init__(self):
        self.events = []

    def on_turn_start(self, line):
        self.events.append(("start", line))

    def on_turn_end(self, answer):
        self.events.append(("end", answer))

    def on_event(self, event):
        self.events.append(("event", type(event).__name__))


class _FakeConvo:
    def __init__(self, answer="hi", boom=None, block=None):
        self._answer = answer
        self._boom = boom
        self._block = block

    async def respond(self, line):
        if self._block is not None:
            await self._block.wait()
        if self._boom is not None:
            raise self._boom
        return self._answer


def test_run_turn_happy_path():
    r = _FakeRenderer()
    polled = []
    asyncio.run(run_turn(_FakeConvo("the answer"), "do x", r,
                         on_turn_complete=lambda: polled.append(1)))
    assert ("start", "do x") in r.events
    assert ("end", "the answer") in r.events
    assert polled == [1]


def test_run_turn_error_is_rendered_not_raised():
    r = _FakeRenderer()
    asyncio.run(run_turn(_FakeConvo(boom=RuntimeError("kaboom")), "x", r))
    assert any(e[0] == "event" for e in r.events)   # error event rendered
    assert ("end", "") in r.events                  # turn closed


class _StatefulRenderer(_FakeRenderer):
    def __init__(self):
        super().__init__()
        from cli.ui.state import SessionState
        self.state = SessionState()
        self.state.status = "running"


def test_run_turn_settles_status_to_ready_on_success():
    r = _StatefulRenderer()
    asyncio.run(run_turn(_FakeConvo("done"), "x", r))
    assert r.state.status == "ready"   # no perpetual "running" at the prompt


def test_run_turn_marks_working_during_respond():
    """Bug D (min liveness): the work status flips to "working" the INSTANT the user
    submits, not only when the first feed event lands — so a slow LLM first-token gap
    isn't dead-silent in the pinned status bar."""
    r = _StatefulRenderer()
    seen = {}

    class _Convo:
        async def respond(self, line):
            seen["status"] = r.state.status
            seen["active"] = r.state.lifecycle.is_active()
            return "ok"

    asyncio.run(run_turn(_Convo(), "x", r))
    assert seen["status"] == "working"   # working before the first event arrives
    assert seen["active"] is True
    assert r.state.status == "ready"     # settles idle when the turn ends


def test_run_turn_errored_turn_ends_on_terminal_status():
    """An errored turn must leave a TERMINAL status (spinner stopped via is_active),
    never a stuck ``working``."""
    r = _StatefulRenderer()
    asyncio.run(run_turn(_FakeConvo(boom=RuntimeError("boom")), "x", r))
    assert r.state.status == "error"   # terminal, not stuck on "working"
    assert r.state.lifecycle.is_active() is False  # spinner stops


def test_run_turn_slash_dispatch_handled_skips_respond():
    r = _FakeRenderer()

    async def _dispatch(line):
        return True  # handled

    asyncio.run(run_turn(_FakeConvo("should not run"), "/help", r,
                         slash_dispatch=_dispatch))
    # No turn render when the slash command was handled.
    assert r.events == []


def test_turn_controller_runs_one_at_a_time_and_interrupts():
    async def _drive():
        r = _FakeRenderer()
        gate = asyncio.Event()
        convo = _FakeConvo(block=gate)
        loop = asyncio.get_event_loop()

        ctrl = TurnController(
            run_coro_factory=lambda line: run_turn(convo, line, r),
            schedule=lambda coro: loop.create_task(coro),
        )
        ctrl.submit("first")
        await asyncio.sleep(0)  # let the task start + block on the gate
        # A second submit while one runs is ignored (one turn at a time).
        ctrl.submit("second")
        assert ("start", "first") in r.events
        assert ("start", "second") not in r.events

        # Interrupt cancels the in-flight turn → renders the interrupt + closes.
        ctrl.interrupt()
        await asyncio.sleep(0.01)
        assert ("end", "") in r.events

    asyncio.run(_drive())


def test_persistent_glue_end_to_end_headless():
    """build_app + TurnController + run_turn wire together: Enter → background
    turn → start/end rendered. The only unverified piece is the live-TTY paint."""
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from cli.ui.app import build_app
    from cli.ui.state import SessionState

    async def _drive():
        state = SessionState()
        r = _FakeRenderer()
        with create_pipe_input() as pipe:
            holder = {}
            app, buf = build_app(
                state,
                on_submit=lambda t: holder["c"].submit(t),
                on_interrupt=lambda: holder["c"].interrupt(),
                output=DummyOutput(),
                input=pipe,
            )
            holder["c"] = TurnController(
                run_coro_factory=lambda line: run_turn(_FakeConvo("ok"), line, r),
                schedule=lambda coro: asyncio.get_event_loop().create_task(coro),
            )
            state._app = app
            app.invalidate()  # feed-callback-style repaint must not raise
            buf.text = "hello"
            buf.validate_and_handle()  # simulate Enter
            await asyncio.sleep(0.02)
        assert ("start", "hello") in r.events
        assert ("end", "ok") in r.events

    asyncio.run(_drive())


def test_turn_controller_ignores_blank():
    calls = []
    ctrl = TurnController(
        run_coro_factory=lambda line: calls.append(line),  # not awaited; blank never reaches
        schedule=lambda coro: calls.append(("scheduled", coro)),
    )
    ctrl.submit("   ")
    assert calls == []
