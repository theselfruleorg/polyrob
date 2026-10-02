"""The chat episode's OUTCOME comes from the closing turn, not from the eviction.

⚠️ The defect (inbox, 2026-09-16 intel review; summary half fixed 2026-09-22, this
is the remaining outcome half): ``cleanup(status=...)`` is called with free-form
strings, and exactly one of them is not an account of the turn.
``task_agent_lite``'s TTL/LRU eviction passes ``"suspended"`` for EVERY session it
pages out — the one that ended mid-thought AND the one whose agent had already
called ``done()`` and answered the owner. ``_CLEANUP_STATUS_TO_OUTCOME`` mapped
both to ``partial``, so a plainly successful chat was recorded as a half-finished
one, and every consumer of the episodic store (the session-start digest, the
continuity bridge, the recap, ``doctor``) read the owner's own conversations as
less complete than they were.

The fix reads the closing turn's own ``done()`` signal
(``AgentHistoryList.is_done()`` — the same accessor ``final_result()`` sits on, and
what the cron/goal episode paths already trust) and lets it UPGRADE the eviction
status. It deliberately does not work the other way round:

* Only ``suspended`` is reconsidered. ``completed``/``error``/``failed``/
  ``cancelled`` are the caller's assertion about the turn and are never overridden.
* ``has_errors()`` is NOT consulted, because ``errors()`` spans the whole history:
  a run that hit a recovered tool error on step 3 and then finished properly is
  done, and downgrading it would trade one wrong label for another.
* Absent or unreadable history stays ``partial``. "I have no evidence it finished"
  is what ``partial`` means, so the fallback is the honest one and no failure in
  this path can change the recorded outcome.
"""
from agents.task.session.cleanup import (
    _CLEANUP_STATUS_TO_OUTCOME,
    _closing_turn_called_done,
    _resolve_chat_outcome,
)


class _FakeHistory:
    def __init__(self, done, raises=False):
        self._done = done
        self._raises = raises

    def is_done(self):
        if self._raises:
            raise RuntimeError("history read blew up during teardown")
        return self._done


class _FakeState:
    def __init__(self, history):
        self.history = history


class _FakeAgent:
    def __init__(self, history=None, is_sub_agent=False):
        if history is not None:
            self.state = _FakeState(history)
        self._is_sub_agent = is_sub_agent


class _FakeOrchestrator:
    def __init__(self, agents):
        self.agents = agents


def _orch(*agents):
    return _FakeOrchestrator({f"a{i}": a for i, a in enumerate(agents)})


# --- the defect itself --------------------------------------------------------- #

def test_an_evicted_session_that_called_done_is_recorded_as_done():
    """The whole point: the owner got their answer, then the session was paged out."""
    orch = _orch(_FakeAgent(_FakeHistory(done=True)))
    assert _resolve_chat_outcome("suspended", orch) == "done"


def test_an_evicted_session_that_never_finished_stays_partial():
    orch = _orch(_FakeAgent(_FakeHistory(done=False)))
    assert _resolve_chat_outcome("suspended", orch) == "partial"


def test_the_old_mapping_still_describes_the_unevidenced_case():
    """`partial` is not retired — it is now what "no done() seen" means."""
    assert _CLEANUP_STATUS_TO_OUTCOME["suspended"] == "partial"


# --- what must NOT be reconsidered --------------------------------------------- #

def test_a_caller_asserted_outcome_is_never_overridden_by_the_history():
    """A session the caller called failed/cancelled is not talked out of it by a
    stale `done()` further up the history."""
    orch = _orch(_FakeAgent(_FakeHistory(done=True)))
    assert _resolve_chat_outcome("failed", orch) == "failed"
    assert _resolve_chat_outcome("error", orch) == "failed"
    assert _resolve_chat_outcome("cancelled", orch) == "cancelled"
    assert _resolve_chat_outcome("completed", orch) == "done"


def test_an_unrecognised_status_still_writes_no_episode():
    """None means "drop it" — the pre-existing refusal to guess, unchanged."""
    orch = _orch(_FakeAgent(_FakeHistory(done=True)))
    assert _resolve_chat_outcome("paged-out-ish", orch) is None
    assert _resolve_chat_outcome("", orch) is None
    assert _resolve_chat_outcome(None, orch) is None


# --- fail-open: nothing here may change an outcome by breaking ------------------ #

def test_a_raising_history_leaves_the_eviction_status_alone():
    orch = _orch(_FakeAgent(_FakeHistory(done=True, raises=True)))
    assert _resolve_chat_outcome("suspended", orch) == "partial"


def test_an_agent_with_no_history_leaves_the_eviction_status_alone():
    orch = _orch(_FakeAgent(history=None))
    assert _resolve_chat_outcome("suspended", orch) == "partial"


def test_an_orchestrator_with_no_agents_leaves_the_eviction_status_alone():
    assert _resolve_chat_outcome("suspended", _FakeOrchestrator({})) == "partial"
    assert _resolve_chat_outcome("suspended", object()) == "partial"


# --- the same agent the summary is read from ----------------------------------- #

def test_the_sub_agents_done_call_is_not_the_sessions_outcome():
    """A delegated sub-agent finishing its errand says nothing about whether the
    conversation with the owner concluded — `_primary_agent` skips sub-agents for
    the summary and must skip them here for the same reason."""
    orch = _orch(_FakeAgent(_FakeHistory(done=True), is_sub_agent=True),
                 _FakeAgent(_FakeHistory(done=False)))
    assert _resolve_chat_outcome("suspended", orch) == "partial"


def test_the_predicate_is_fail_closed_on_its_own():
    assert _closing_turn_called_done(_FakeAgent(_FakeHistory(done=True))) is True
    assert _closing_turn_called_done(_FakeAgent(_FakeHistory(done=False))) is False
    assert _closing_turn_called_done(_FakeAgent(history=None)) is False
    assert _closing_turn_called_done(None) is False
