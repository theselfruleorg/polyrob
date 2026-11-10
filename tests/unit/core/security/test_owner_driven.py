"""One answer to "does this act need a tap": the owner's clean turn or standing job."""
from types import SimpleNamespace

import pytest

from core import autonomy_marker as am
from core.security.owner_turn import is_owner_driven


def _ctx(sid, **kw):
    base = dict(session_id=sid, user_id="u1", role="orchestrator", is_sub_agent=False, metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)


def test_the_owner_typing_now():
    assert is_owner_driven(_ctx("typed"))


def test_a_recorded_job_run():
    am.mark_autonomous("job-run", cron_job_id="job-1")
    am.note_job("job-1", "post the notice")
    assert is_owner_driven(_ctx("job-run"))


def test_an_unrecorded_autonomous_run_is_not():
    am.mark_autonomous("planner-run")
    assert not is_owner_driven(_ctx("planner-run"))


@pytest.mark.parametrize("kw", [
    {"metadata": {"correspondent_tainted": True}},
    {"metadata": {"turn_kind": "group"}},
    {"metadata": {"turn_kind": "self_wake"}},
    {"role": "leaf"},
    {"is_sub_agent": True},
    {"user_id": "someone-else"},
])
def test_a_turn_that_is_not_the_owners_is_not(kw):
    assert not is_owner_driven(_ctx("typed-2", **kw))
    am.mark_autonomous("job-run-2", cron_job_id="job-2")
    am.note_job("job-2", "post the notice")
    assert not is_owner_driven(_ctx("job-run-2", **kw))


def test_no_context_is_not():
    assert not is_owner_driven(None)
