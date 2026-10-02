"""W2-C — background-review fork: cadence decision + gating + sub-agent exemption."""
import types

import pytest

from agents.task.agent.core.background_review import BackgroundReviewMixin


class _Host(BackgroundReviewMixin):
    """Minimal host exposing just what _bg_review_should_fire reads."""
    def __init__(self, is_sub=False):
        self._is_sub_agent = is_sub
        self._bg_review_productive_turns = 0


def test_disabled_never_fires(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "false")
    h = _Host()
    assert all(not h._bg_review_should_fire(True) for _ in range(20))


def test_fires_every_interval(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "3")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    fires = [h._bg_review_should_fire(True) for _ in range(7)]
    # fire on the 3rd and 6th productive turns
    assert fires == [False, False, True, False, False, True, False]


def test_unproductive_turns_dont_count(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "2")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    assert h._bg_review_should_fire(False) is False
    assert h._bg_review_should_fire(False) is False
    assert h._bg_review_should_fire(True) is False  # 1st productive
    assert h._bg_review_should_fire(True) is True   # 2nd productive -> fire


def test_sub_agents_exempt(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host(is_sub=True)
    assert h._bg_review_should_fire(True) is False  # a reviewer never forks a reviewer


def test_spawn_is_fail_open(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    h.orchestrator = types.SimpleNamespace(session_id="s")
    # no event loop running create_task would normally raise; the method must swallow it
    h._maybe_spawn_background_review(turn_was_productive=True)  # must not raise


def test_review_prompt_excludes_self_context_when_flag_off(monkeypatch):
    # polyrob Phase E: the reviewer only proposes SELF-context refinements when
    # SELF_CONTEXT_WRITABLE is on. Off => skills-only prompt (byte-identical legacy).
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "false")
    from agents.task.agent.core.background_review import build_review_prompt
    p = build_review_prompt()
    assert "self_context_manage" not in p
    assert "skill_manage" in p


def test_review_prompt_includes_self_context_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "true")
    from agents.task.agent.core.background_review import build_review_prompt
    p = build_review_prompt()
    assert "self_context_manage" in p
    # it must frame the proposal as quarantined/consolidated, not auto-applied
    assert "quarantin" in p.lower() or "review" in p.lower()


# --- the reviewer sees a bounded digest of the parent's steps ------------------
# The child is a fresh sub-agent session: without a digest in its task it saw
# nothing of the "recent conversation/work" the prompt told it to review.

def _step(memory, action_name, content=None, error=None):
    class _Act:
        def model_dump(self, exclude_none=True):
            return {action_name: {"x": 1}}
    brain = types.SimpleNamespace(memory=memory)
    out = types.SimpleNamespace(current_state=brain, action=[_Act()])
    res = types.SimpleNamespace(extracted_content=content, error=error)
    return types.SimpleNamespace(model_output=out, result=[res])


def test_review_digest_is_bounded_and_keeps_the_recent_steps():
    from agents.task.agent.core.background_review import build_review_digest
    items = [_step(f"did thing {i}", "filesystem_write_file", content="ok " * 200)
             for i in range(30)]
    d = build_review_digest(items, max_steps=5, max_chars=900)
    assert len(d) <= 900
    assert "did thing 29" in d and "did thing 0 " not in d
    assert "filesystem_write_file" in d


def test_review_digest_empty_history_is_empty():
    from agents.task.agent.core.background_review import build_review_digest
    assert build_review_digest(None) == ""
    assert build_review_digest([]) == ""


def test_review_prompt_carries_the_digest_framed_as_data():
    from agents.task.agent.core.background_review import build_review_prompt
    p = build_review_prompt("step 1: memory: wrote report.md")
    assert "wrote report.md" in p
    assert "parent_step_digest" in p  # untrusted-data frame
    assert "No step digest" in build_review_prompt()


def test_run_background_review_passes_the_digest_to_the_child():
    import asyncio
    from agents.task.agent.core.background_review import BackgroundReviewMixin

    seen = {}

    class _Mgr:
        async def run_subtask(self, **kw):
            seen.update(kw)

    class _H(BackgroundReviewMixin):
        agent_id = "a"
        llm = object()
        _judge_llm = object()
        orchestrator = types.SimpleNamespace(get_sub_agent_manager=lambda: _Mgr())
        history = types.SimpleNamespace(history=[_step("drafted the pricing post",
                                                       "filesystem_write_file")])

    asyncio.run(_H()._run_background_review())
    assert "drafted the pricing post" in seen["task"]
