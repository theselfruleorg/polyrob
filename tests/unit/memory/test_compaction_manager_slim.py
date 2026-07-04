from modules.memory.task.compaction_manager import CompactionManager


def test_should_compact_kept():
    assert hasattr(CompactionManager, "should_compact_messages")
    assert hasattr(CompactionManager, "for_model")


def test_action_half_removed():
    for dead in ("compact_messages", "clear_old_tool_results", "compact_recent_steps",
                 "archive_completed_phases", "get_compaction_stats"):
        assert not hasattr(CompactionManager, dead), f"{dead} is dead — only detection is used"
