"""TDD tests verifying vestigial ContextManager is removed from MemoryManager."""
import importlib
import inspect

import pytest


def test_context_manager_gone():
    """modules.memory.context_manager must not exist after the trim."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("modules.memory.context_manager")


def test_memory_manager_still_holds_tcm():
    """MemoryManager.__init__ must still wire task_context_manager but not ContextManager."""
    from modules.memory.memory_manager import MemoryManager
    src = inspect.getsource(MemoryManager.__init__)
    assert "task_context_manager" in src, "MemoryManager must still build task_context_manager"
    assert "ContextManager(" not in src, "ContextManager wiring must be removed"
