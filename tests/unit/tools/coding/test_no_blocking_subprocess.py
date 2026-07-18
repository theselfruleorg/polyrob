"""Regression (P1 finalization): the coding tool ran blocking subprocess.run
(git snapshot, lsp checker) and an unbounded agent regex (grep) directly on the
event loop, freezing the whole worker. The blocking work is now offloaded to a
thread — the snapshot/diagnostics helpers are async, and grep awaits search_files
via asyncio.to_thread.
"""
import inspect

from tools.coding.tool import CodingTool


def test_snapshot_and_diagnostics_helpers_are_async():
    assert inspect.iscoroutinefunction(CodingTool._snapshot_before_edit)
    assert inspect.iscoroutinefunction(CodingTool._with_diagnostics)


def test_grep_offloads_search_to_thread():
    src = inspect.getsource(CodingTool.grep)
    assert "to_thread" in src, "grep must offload the agent-supplied regex search off the loop"
    # The blocking search must not be called directly (bare) on the loop.
    assert "await asyncio.to_thread(" in src
