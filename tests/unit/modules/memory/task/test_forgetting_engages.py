"""The forgetting cap default must be low enough that pruning engages in normal sessions.

The retriever only ever displays ~15 findings; if the storage cap stays at 500,
`_check_and_prune_memories` never fires and importance-based forgetting is dead code.
This pins the *production* default (read from source) so a silent regression to 500 fails.
"""
import inspect
import re

from modules.memory.task.task_context_manager import TaskContextManager


def test_default_max_findings_engages_pruning():
    src = inspect.getsource(TaskContextManager.__init__)
    m = re.search(
        r'max_findings_per_phase\s*=\s*config\.get\(\s*["\']MAX_FINDINGS_PER_PHASE["\']\s*,\s*(\d+)\)',
        src,
    )
    assert m, "MAX_FINDINGS_PER_PHASE default not found in TaskContextManager.__init__"
    default = int(m.group(1))
    # Retriever displays ~15; storage cap must be a small multiple, not 500, or pruning never runs.
    assert default <= 100, (
        f"forgetting cap default is {default}; too high — _check_and_prune_memories won't engage"
    )
