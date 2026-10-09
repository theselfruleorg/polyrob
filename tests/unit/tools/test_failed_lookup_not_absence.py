"""A failed lookup is evidence about the search, not proof of absence.

Prod 2026-10-03: `filesystem_read_file("changelog-1.0.1-1.0.3.md")` at the
workspace root returned "File not found" while the file sat under reports/;
`coding_grep` with the file NAME as a regex then said "(no matches)" (it
searches contents). The agent told its owner the file "never existed", and
memory_writer promoted that claim into seven cross-session rows.
"""
import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from tools.filesystem import FileSystem, ReadFileAction


async def _noop():
    return None


@pytest.fixture
def tool(tmp_path, monkeypatch):
    from agents.task.path import PathManager, set_path_manager
    set_path_manager(PathManager(data_root=str(tmp_path / "data")))
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    monkeypatch.delenv("AUTONOMOUS_READ_PAGE_LINES", raising=False)
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-nf")
    t.session_id, t.user_id, t._current_session_id, t._enabled = "s1", "u1", None, True
    return t


def _put(tool, rel, text="x\n"):
    p = tool._normalize_path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


@pytest.mark.asyncio
async def test_read_not_found_names_the_path_and_suggests_the_same_name(tool):
    _put(tool, "reports/changelog-1.0.1-1.0.3.md")
    _put(tool, "node_modules/changelog-1.0.1-1.0.3.md")  # skipped dir
    with pytest.raises(Exception) as ei:
        await tool.read_file(ReadFileAction(file_path="changelog-1.0.1-1.0.3.md"))
    msg = str(ei.value)
    assert "File not found: changelog-1.0.1-1.0.3.md" in msg
    assert "looked at workspace path: changelog-1.0.1-1.0.3.md" in msg
    assert "Did you mean: reports/changelog-1.0.1-1.0.3.md?" in msg
    assert "node_modules" not in msg
    assert "not proof the file does not exist elsewhere" in msg


@pytest.mark.asyncio
async def test_read_not_found_with_no_match_says_the_whole_workspace_was_searched(tool):
    _put(tool, "reports/other.md")
    with pytest.raises(Exception) as ei:
        await tool.read_file(ReadFileAction(file_path="missing.md"))
    msg = str(ei.value)
    assert "Did you mean" not in msg
    assert "No file with that name anywhere in the workspace" in msg


def test_basename_walk_is_bounded(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i}.txt").write_text("x")
    (tmp_path / "zz").mkdir()
    (tmp_path / "zz" / "target.md").write_text("x")
    hits, complete = FileSystem._find_by_basename(str(tmp_path), "target.md", max_entries=5)
    assert complete is False  # a capped walk never claims "searched everything"


def test_grep_descriptions_say_contents_not_names():
    from tools.coding.tool import GrepParams
    assert "CONTENTS" in GrepParams.model_fields["pattern"].description
    assert "BY NAME" in GrepParams.model_fields["glob"].description


def test_prompt_says_a_failed_lookup_is_not_proof_of_absence():
    from agents.task.agent.prompts import SystemPrompt
    sp = SystemPrompt.__new__(SystemPrompt)
    sp._catalog_pinned = lambda: True
    text = sp._get_source_precedence_content()
    assert "not proof of absence" in text
    assert "never existed" in text


# --- memory: a finding written around a failed tool call is marked -----------

class _TCM:
    def __init__(self):
        self.findings = []

    def add_step_memory(self, **kw):
        self.findings.append(kw["finding"])
        return True

    def drain_promoted_findings(self, session_id):
        return []


def _host():
    from agents.task.agent.core.memory_writer import MemoryWriterMixin

    class _H(MemoryWriterMixin):
        pass
    h = _H()
    h.task_context_manager, h.session_id, h.user_id = _TCM(), "s1", "u1"
    h.logger, h.task = logging.getLogger("mw"), ""
    h.state = SimpleNamespace(track_finding=lambda: None)
    return h


def _step(h, n, memory, results):
    asyncio.run(h._save_step_to_memory(step_number=n, brain_state={"memory": memory},
                                       actions=[], results=results))


def test_finding_after_a_failed_step_is_marked_unverified():
    h = _host()
    _step(h, 1, "Reading the changelog file now.", [SimpleNamespace(error="File not found: c.md")])
    _step(h, 2, "The changelog file is not on disk; it never existed.", [SimpleNamespace(error=None)])
    _step(h, 3, "Wrote the summary to reports/summary.md.", [SimpleNamespace(error=None)])
    f = h.task_context_manager.findings
    assert f[0].startswith("[unverified] ")
    assert f[1].startswith("[unverified] ")  # the brain saw step 1's failure
    assert not f[2].startswith("[unverified]")


def test_strategic_clue_is_not_promoted_to_cross_session_memory():
    from modules.memory.task.task_context_manager import TaskContextManager
    from modules.memory.task.phase_manager import STRATEGIC_CLUE_PREFIX
    pm = SimpleNamespace(key_findings=[f"{STRATEGIC_CLUE_PREFIX}DISCOVERY] Discovery: 2 insights.",
                                       "Real finding about the repo."])
    session = SimpleNamespace(memory=SimpleNamespace(phase_memories=[pm]))
    tcm = object.__new__(TaskContextManager)
    tcm._sessions = {"s1": session}
    assert tcm.drain_promoted_findings("s1") == ["Real finding about the repo."]


# --- step telemetry: a failed read is not a read -----------------------------

def test_files_read_counts_only_successful_actions():
    from agents.task.agent.core.step_telemetry import StepTelemetryMixin
    captured = {}

    class _H(StepTelemetryMixin):
        pass
    h = _H()
    h.task, h._last_result = "t", []
    h.state = SimpleNamespace(consecutive_failures=0, n_steps=1)
    h.message_manager = SimpleNamespace()
    h.telemetry_manager = SimpleNamespace(capture_step=lambda **kw: captured.update(kw),
                                          emit_event=lambda *a, **k: None)
    h.logger = logging.getLogger("st")
    actions = [{"filesystem_read_file": {"file_path": "missing.md"}},
               {"filesystem_read_file": {"file_path": "reports/ok.md"}}]
    mo = SimpleNamespace(action=actions, current_state=SimpleNamespace())
    res = [SimpleNamespace(error="File not found: missing.md", extracted_content=None,
                           is_done=False, include_in_memory=False),
           SimpleNamespace(error=None, extracted_content="x", is_done=False,
                           include_in_memory=False)]
    asyncio.run(h._emit_step_telemetry(mo, None, res))
    assert captured["files_read"] == ["reports/ok.md"]
