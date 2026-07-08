"""§7.7 — session-close reflection trigger for the autonomous workload.

The per-step reflection trigger fires only at 25 findings in ONE session (an
in-memory counter that resets per session), so short cron/goal sessions never reach
it (reflection_consolidate=0 in prod). A session-close trigger consolidates a short
session's handful of findings at cleanup, at a lower threshold, so autonomous runs
actually reflect. Gated REFLECTION_ON_SESSION_CLOSE (default OFF).
"""
import tempfile
from unittest.mock import MagicMock

import pytest

from modules.memory.task.task_context_manager import TaskContextManager


class FakeConfig:
    def __init__(self, values: dict):
        self.data = dict(values)

    def get(self, key, default=None):
        return self.data.get(key, default)


def _make_manager(values):
    data_dir = tempfile.mkdtemp()
    base = {
        "HIERARCHICAL_MEMORY_ENABLED": True,
        "SEMANTIC_RETRIEVAL_ENABLED": False,
        "DATA_DIR": data_dir,
        "DATA_PATH": data_dir,
    }
    base.update(values)
    cfg = FakeConfig(base)
    mgr = TaskContextManager(name="test_manager", config=cfg)
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


# T2-04: the flag is read from the ENVIRONMENT (core.env SSOT), NOT config.get —
# BotConfig.get never reads env for the undeclared REFLECTION_ON_SESSION_CLOSE key, so
# these tests must configure via monkeypatch.setenv to exercise the real production path.
# (Setting it in FakeConfig was the false-confidence trap: FakeConfig.get honors any key,
# unlike the real BotConfig.get, so the tests passed while production was dead.)

def test_close_triggers_reflection_below_step_threshold_when_enabled(monkeypatch):
    monkeypatch.setenv("REFLECTION_ON_SESSION_CLOSE", "true")
    monkeypatch.setenv("REFLECTION_SESSION_CLOSE_THRESHOLD", "5")
    mgr = _make_manager({})
    mgr.create_session(session_id="s1", task="t")
    _add_findings(mgr, "s1", 6)  # below the per-step threshold of 25
    mgr._trigger_reflection = MagicMock()
    assert mgr.close_session("s1") is True
    mgr._trigger_reflection.assert_called_once()


def test_close_no_reflection_when_flag_off(monkeypatch):
    monkeypatch.delenv("REFLECTION_ON_SESSION_CLOSE", raising=False)
    mgr = _make_manager({})
    mgr.create_session(session_id="s2", task="t")
    _add_findings(mgr, "s2", 6)
    mgr._trigger_reflection = MagicMock()
    mgr.close_session("s2")
    mgr._trigger_reflection.assert_not_called()


def test_close_no_reflection_below_min_findings(monkeypatch):
    monkeypatch.setenv("REFLECTION_ON_SESSION_CLOSE", "true")
    monkeypatch.setenv("REFLECTION_SESSION_CLOSE_THRESHOLD", "5")
    mgr = _make_manager({})
    mgr.create_session(session_id="s3", task="t")
    _add_findings(mgr, "s3", 2)  # fewer than the close threshold -> no cost
    mgr._trigger_reflection = MagicMock()
    mgr.close_session("s3")
    mgr._trigger_reflection.assert_not_called()


def test_flag_reads_env_not_config_get(monkeypatch):
    """T2-04 regression: the flag must come from the environment. A value present ONLY
    in the config object (undeclared BotConfig field) must NOT enable it, while the env
    var MUST. This is what the dead-gate bug got wrong."""
    monkeypatch.delenv("REFLECTION_ON_SESSION_CLOSE", raising=False)
    # config-only (the old, dead path) => stays OFF
    mgr_cfg_only = _make_manager({"REFLECTION_ON_SESSION_CLOSE": True})
    assert mgr_cfg_only.reflection_on_session_close is False
    # env set => ON
    monkeypatch.setenv("REFLECTION_ON_SESSION_CLOSE", "true")
    mgr_env = _make_manager({})
    assert mgr_env.reflection_on_session_close is True
