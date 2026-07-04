"""Guard test: CompactionManager must not carry attrs orphaned by action-half deletion.

The action methods (compact_messages, clear_old_tool_results, compact_recent_steps,
archive_completed_phases) were removed in a prior task.  tool_result_max_age and
max_recent_steps were their sole consumers; after the deletion those attrs are
dead weight that misleads readers and adds noise to __init__ signatures.
"""
from modules.memory.task.compaction_manager import CompactionManager


def test_detection_still_works():
    cm = CompactionManager(soft_threshold=0.35, hard_threshold=0.45)
    should, ratio = cm.should_compact_messages(50, 100)
    assert should is True and ratio == 0.5
    should2, _ = cm.should_compact_messages(10, 100)
    assert should2 is False


def test_orphaned_attrs_removed():
    cm = CompactionManager()
    assert not hasattr(cm, "tool_result_max_age"), "orphaned after action-half deletion"
    assert not hasattr(cm, "max_recent_steps"), "orphaned after action-half deletion"
