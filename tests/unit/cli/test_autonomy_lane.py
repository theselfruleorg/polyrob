"""098 CL-02: the REPL autonomy lane has an exit for every way a run ends."""
import asyncio

from cli.ui.autonomy_live import LiveAutonomy
from cli.ui.lifecycle import TurnLifecycle


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_cancelled_background_run_clears_on_ttl():
    clk = _Clock()
    lc = TurnLifecycle(clock=clk)
    lc.begin_background("s1")              # the cap cancels it: no SessionDone
    assert lc.autonomy_busy() is True
    clk.t += TurnLifecycle.BACKGROUND_TTL_SEC + 1
    assert lc.autonomy_busy() is False


def test_autonomy_off_resets_lane():
    lc = TurnLifecycle()
    lc.begin_background("s1")

    class _H:
        async def stop(self):
            pass

    live = LiveAutonomy(lambda: _H(), handles=_H(), on_stopped=lc.reset_background)
    assert asyncio.run(live.stop()) is True
    assert lc.autonomy_busy() is False


def test_duplicate_start_does_not_double_count():
    lc = TurnLifecycle()
    lc.begin_background("s1")
    lc.begin_background("s1")
    lc.end_background("s1")
    assert lc.autonomy_busy() is False


def test_two_sessions_tracked_apart():
    lc = TurnLifecycle()
    lc.begin_background("s1")
    lc.begin_background("s2")
    lc.end_background("s1")
    assert lc.autonomy_busy() is True
    lc.end_background("s2")
    assert lc.autonomy_busy() is False
