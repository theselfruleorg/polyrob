"""Coherency review 2026-10-13 E-A7: an owner message over
USER_MESSAGE_TRUNCATE_LENGTH lost its middle with no way to read it. The full
text is now saved to workspace/inbox/<ts>.md and the elision names that file."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
import agents.task.path as path_mod
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="t", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=8000,
                          session_id="s-inbox")


def _pm(tmp_path, project_root=False):
    return SimpleNamespace(is_project_root_workspace=project_root,
                           get_workspace_dir=lambda sid, user_id=None: tmp_path)


def _long():
    return "SPEC START " + "".join(f"clause {i}; " for i in range(800)) + " SPEC END"


def test_long_owner_message_is_saved_in_full_and_named(tmp_path, monkeypatch):
    monkeypatch.setattr(path_mod, "pm", lambda: _pm(tmp_path))
    mm = _mm()
    text = _long()
    mm.inject_user_guidance([{"text": text, "kind": "comment", "metadata": {}}],
                            session_context={"continuation": True})
    frame = mm.history.messages[-1].message.content
    saved = list((tmp_path / "inbox").iterdir())
    assert len(saved) == 1 and saved[0].suffix == ".md"
    assert saved[0].read_text() == text
    # the elision names the REAL action (there is no bare `read_file` action)
    assert f'filesystem_read_file(file_path="inbox/{saved[0].name}")' in frame
    assert "clause 400;" not in frame  # the middle is elided from the context


def test_short_message_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(path_mod, "pm", lambda: _pm(tmp_path))
    mm = _mm()
    mm.inject_user_guidance([{"text": "hi", "kind": "comment", "metadata": {}}],
                            session_context={"continuation": True})
    assert not (tmp_path / "inbox").exists()


def test_project_root_workspace_is_never_written(tmp_path, monkeypatch):
    monkeypatch.setattr(path_mod, "pm", lambda: _pm(tmp_path, project_root=True))
    mm = _mm()
    mm.inject_user_guidance([{"text": _long(), "kind": "comment", "metadata": {}}],
                            session_context={"continuation": True})
    assert not (tmp_path / "inbox").exists()
    assert "chars elided" in mm.history.messages[-1].message.content


def test_a_child_writes_into_the_real_workspace_not_its_virtual_id(tmp_path, monkeypatch):
    """A child's MessageManager carries the VIRTUAL session id; the inbox file
    must land in the orchestrator's workspace (MessageManager.workspace_root),
    the one the filesystem tool reads."""
    virtual_dir = tmp_path / "virtual"
    real_ws = tmp_path / "real"
    real_ws.mkdir()
    monkeypatch.setattr(path_mod, "pm", lambda: _pm(virtual_dir))
    mm = _mm()
    mm.session_id = "s-inbox__sub_1"
    mm.workspace_root = str(real_ws)
    text = _long()
    mm.inject_user_guidance([{"text": text, "kind": "comment", "metadata": {}}],
                            session_context={"continuation": True})
    saved = list((real_ws / "inbox").iterdir())
    assert len(saved) == 1 and saved[0].read_text() == text
    assert not virtual_dir.exists()
    assert f'filesystem_read_file(file_path="inbox/{saved[0].name}")' in \
        mm.history.messages[-1].message.content


def test_construction_stamps_the_orchestrator_workspace():
    import inspect
    from agents.task.agent.core import construction
    assert 'message_manager.workspace_root = getattr(self.orchestrator, "workspace_dir"' \
        in inspect.getsource(construction)
