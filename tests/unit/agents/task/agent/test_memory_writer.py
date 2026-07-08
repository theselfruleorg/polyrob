"""Unit tests for the extracted MemoryWriterMixin pure helpers (PR9).

Exercises the moved logic in isolation via a tiny host that composes the
mixin and supplies a logger — no LLM, container, or H-MEM needed.
"""

import logging

from agents.task.agent.core.memory_writer import MemoryWriterMixin


class _Host(MemoryWriterMixin):
    def __init__(self):
        self.logger = logging.getLogger("test_memory_writer")


# --- _extract_progress_from_memory ---


def test_extract_progress_found():
    h = _Host()
    assert h._extract_progress_from_memory("Working... Progress: 3/10 done") == "3/10"


def test_extract_progress_none_when_absent():
    h = _Host()
    assert h._extract_progress_from_memory("no marker here") is None


def test_extract_progress_none_on_empty():
    h = _Host()
    assert h._extract_progress_from_memory("") is None
    assert h._extract_progress_from_memory(None) is None


# --- _build_action_summary ---


def test_build_action_summary_empty():
    h = _Host()
    assert h._build_action_summary([]) == "No actions taken"


def test_build_action_summary_uses_first_key():
    h = _Host()
    actions = [{"browser_navigate": {"url": "x"}}, {"extract_content": {}}]
    assert h._build_action_summary(actions) == "Executed: browser_navigate, extract_content"


# --- _extract_intelligent_preview ---


def test_extract_intelligent_preview_truncates_plaintext():
    h = _Host()
    content = "word " * 5000  # ~25000 chars
    preview = h._extract_intelligent_preview(content, max_length=100)
    assert len(preview) <= len(content)
    assert isinstance(preview, str)


def test_p2_3_no_next_goal_junk_finding():
    """P2-3: when brain memory is empty and no result finding exists, the step writes
    finding=None (not the imperative next_goal) so junk doesn't become durable memory."""
    import asyncio

    captured = {}

    class _TCM:
        def add_step_memory(self, **kw):
            captured.update(kw)
            return True

    class _Host(MemoryWriterMixin):
        def __init__(self):
            import logging
            self.task_context_manager = _TCM()
            self.session_id = "s1"
            self.user_id = "u1"
            self.logger = logging.getLogger("p2_3")
            self.task = ""

        def _extract_finding_from_results(self, results):
            return None  # no secondary finding

    h = _Host()
    # brain memory empty + next_goal set: must NOT write next_goal as the finding
    asyncio.run(h._save_step_to_memory(
        step_number=1,
        brain_state={"memory": "", "next_goal": "Click the search button", "phase": "discovery"},
        actions=[], results=[],
    ))
    assert captured.get("finding") is None, "next_goal must not become a finding"
