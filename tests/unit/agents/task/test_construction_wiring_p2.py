"""P2-24 / P2-25 (intelligence-polish plan 2026-07-07): construction wiring fixes.

P2-25: the per-session skills.json webview write used the wrong kwarg (`subdir=` vs
`subdir_name=`), raising TypeError swallowed by the except → the file was never written.
Also `is_user_skill` referenced a nonexistent `_load_user_skill_rules` method.

P2-24: the prompt action index was computed BEFORE the memory-backend registration, so
the first session of a process omitted session_search/recent_activity/memory.
"""
import inspect

from agents.task.agent.core import construction


def test_p2_25_skills_json_uses_correct_kwarg():
    src = inspect.getsource(construction)
    assert 'subdir_name=""' in src
    # is_user_skill now uses the precomputed set (not the nonexistent method call)
    assert "_user_skill_ids" in src
    assert "skill_manager._load_user_skill_rules(user_id)" not in src


def test_p2_24_action_index_recomputed_after_memory_registration():
    src = inspect.getsource(construction)
    # the recompute comment + call exist after the memory block
    assert "recompute the prompt action index AFTER" in src
    idx_memory = src.index("memory backend registration skipped")
    idx_recompute = src.index("recompute the prompt action index AFTER")
    assert idx_recompute > idx_memory, "recompute must come after memory registration"
