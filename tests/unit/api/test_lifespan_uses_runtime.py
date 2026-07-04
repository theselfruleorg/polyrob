import inspect
import api.app as app


def test_lifespan_delegates_to_start_autonomy():
    src = inspect.getsource(app.lifespan)
    assert "start_autonomy" in src
    assert "build_cron_ticker" not in src
    assert "build_goal_ticker" not in src
    assert "build_curator_ticker" not in src
