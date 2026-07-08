"""P0-7 (intelligence-polish plan 2026-07-07): session-close reflection output must
reach the cross-session store, not be discarded.

The close path used to (a) run reflection AFTER save_session — so the disk copy
predated the consolidated summary — and (b) let the pending reflection summaries die
with `del self._sessions[...]`, never draining them cross-session. close_session_and_drain
now runs reflection BEFORE save and returns the drained promoted findings for the
caller to sync.
"""
import tempfile
from unittest.mock import MagicMock

from modules.memory.task.task_context_manager import TaskContextManager


class FakeConfig:
	def __init__(self, values: dict):
		self.data = dict(values)

	def get(self, key, default=None):
		return self.data.get(key, default)


def _make_manager(values=None):
	data_dir = tempfile.mkdtemp()
	base = {
		"HIERARCHICAL_MEMORY_ENABLED": True,
		"SEMANTIC_RETRIEVAL_ENABLED": False,
		"DATA_DIR": data_dir,
		"DATA_PATH": data_dir,
	}
	base.update(values or {})
	mgr = TaskContextManager(name="test_manager", config=FakeConfig(base))
	stub = MagicMock()
	stub.has_service.return_value = True
	mgr.container = stub
	return mgr


def _add_findings(mgr, session_id, n):
	for i in range(n):
		mgr.add_step_memory(
			session_id=session_id, step=i + 1,
			brain_state={"phase": "discovery", "memory": "x", "next_goal": "y"},
			action_summary=f"act {i}", finding=f"finding {i}", total_steps=50)


def test_drain_returns_promoted_findings_on_close():
	"""close_session_and_drain returns the session's promoted findings before del."""
	mgr = _make_manager()
	mgr.create_session(session_id="s1", task="t")
	_add_findings(mgr, "s1", 4)
	ok, drained = mgr.close_session_and_drain("s1", "u1")
	assert ok is True
	# the 4 findings were never synced during this (test) session → all drain now
	assert any("finding" in d for d in drained)
	assert "s1" not in mgr._sessions  # session released


def test_reflection_runs_before_save_and_summary_drains(monkeypatch):
	"""When close-reflection is enabled + threshold met, the consolidated summary is
	queued and comes back in the drained list (so the caller can sync it)."""
	monkeypatch.setenv("REFLECTION_ON_SESSION_CLOSE", "true")
	monkeypatch.setenv("REFLECTION_SESSION_CLOSE_THRESHOLD", "3")
	mgr = _make_manager()
	mgr.create_session(session_id="s2", task="t")
	_add_findings(mgr, "s2", 5)

	call_order = []
	real_save = mgr.save_session

	def _tracked_save(sid, uid=None):
		call_order.append("save")
		return real_save(sid, uid)

	def _fake_trigger(session_id, phase):
		call_order.append("reflect")
		sd = mgr._sessions[session_id]
		pending = getattr(sd, "_pending_reflection_summaries", None) or []
		pending.append("[phase-summary:discovery] consolidated insight")
		sd._pending_reflection_summaries = pending

	monkeypatch.setattr(mgr, "save_session", _tracked_save)
	monkeypatch.setattr(mgr, "_trigger_reflection", _fake_trigger)

	ok, drained = mgr.close_session_and_drain("s2", "u1")
	assert ok is True
	# reflection BEFORE save (so the persisted summary is up to date)
	assert call_order.index("reflect") < call_order.index("save")
	# the consolidated summary reaches the caller for cross-session sync
	assert any("consolidated insight" in d for d in drained)


def test_close_session_bool_wrapper_still_works():
	"""Back-compat: close_session returns a bool and releases the session."""
	mgr = _make_manager()
	mgr.create_session(session_id="s3", task="t")
	_add_findings(mgr, "s3", 2)
	assert mgr.close_session("s3", "u1") is True
	assert mgr.close_session("s3", "u1") is False  # already gone
