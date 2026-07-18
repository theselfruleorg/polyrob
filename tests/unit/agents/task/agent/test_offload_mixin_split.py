"""P7 finalization: the large-tool-result file-offload concern was split out of
MemoryWriterMixin (which mixed H-MEM writing with unrelated offload logic) into a
dedicated ToolResultOffloadMixin. The Agent still composes both, so behavior is
identical; this guards the separation.
"""
from agents.task.agent.core.memory_writer import MemoryWriterMixin
from agents.task.agent.core.result_offload import ToolResultOffloadMixin


def test_offload_methods_live_on_offload_mixin_not_memory_writer():
    offload = {"_extract_intelligent_preview", "_result_is_untrusted", "_handle_large_action_results"}
    for m in offload:
        assert m in ToolResultOffloadMixin.__dict__, f"{m} must live on ToolResultOffloadMixin"
        assert m not in MemoryWriterMixin.__dict__, f"{m} must NOT remain on MemoryWriterMixin"


def test_memory_writer_keeps_its_own_methods():
    for m in ("_save_step_to_memory", "_build_action_summary", "_build_memory_from_actions"):
        assert m in MemoryWriterMixin.__dict__


def test_agent_composes_both_mixins():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, MemoryWriterMixin)
    assert issubclass(Agent, ToolResultOffloadMixin)
    # the offload methods resolve on the composed Agent
    assert hasattr(Agent, "_handle_large_action_results")
