"""Test isolation for the goals suite.

``agents.task.goals.autonomy_marker`` keeps an in-process, module-global set of
autonomous session ids. Goal-dispatcher tests (e.g. ``test_dispatcher_refusal``) run
``_run_goal`` which marks their fake session id (``"s1"``) autonomous — and that leaks
into any later test that reuses the same session id (the self-context promote action
tests hardcode ``session_id="s1"`` and were being misread as forged/autonomous). Reset
the marker around every goals test so the global never bleeds across tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_autonomy_marker():
    from agents.task.goals import autonomy_marker
    autonomy_marker._SESSIONS.clear()
    yield
    autonomy_marker._SESSIONS.clear()
