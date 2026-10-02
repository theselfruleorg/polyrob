"""058 T1.5 — numpy/apsw/sqlite-vec are extras; H-MEM degrades to lexical and says so.

``semantic_retriever.py`` was a module-level ``import numpy`` and
``task_context_manager.py`` imported it at module level, so numpy loaded for
every agent on every install. With numpy blocked: the package still imports,
the retriever is the lexical one (the EXISTING fallback — no second one), and
the log names numpy and the extra.
"""
import builtins
import importlib
import logging
import sys

import pytest


@pytest.fixture
def no_numpy(monkeypatch):
    """Evict numpy + the H-MEM package so their module-level imports re-run
    under a blocked numpy; on teardown put the ORIGINAL modules back, so a
    later test's ``monkeypatch.setattr("modules.memory.task.threat_scan…")``
    resolves the same objects everyone else imported."""
    evicted = {m: sys.modules[m] for m in list(sys.modules)
               if m == "numpy" or m.startswith("numpy.") or m.startswith("modules.memory.task")}
    for m in evicted:
        del sys.modules[m]
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("No module named 'numpy' (blocked by test)")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    try:
        yield
    finally:
        for m in list(sys.modules):
            if m.startswith("modules.memory.task"):
                del sys.modules[m]
        sys.modules.update(evicted)
        # A re-import also rebound the PARENT package's attribute
        # (modules.memory.task = <new pkg>); point it back at the original so a
        # dotted monkeypatch path resolves the same object sys.modules holds.
        for name, mod in evicted.items():
            parent, _, child = name.rpartition(".")
            if parent and parent in sys.modules:
                setattr(sys.modules[parent], child, mod)


def test_package_imports_without_numpy(no_numpy):
    pkg = importlib.import_module("modules.memory.task")
    assert hasattr(pkg, "TaskContextManager")


def test_semantic_retriever_refuses_naming_the_extra(no_numpy):
    from modules.memory.task.semantic_retriever import SemanticRetriever

    class _Holder:
        embedding_model = object()

    with pytest.raises(ImportError, match=r"numpy.*polyrob\[memory-vector\]"):
        SemanticRetriever(rag_manager=_Holder())


def test_context_manager_falls_back_to_lexical_and_logs_numpy(no_numpy, monkeypatch, caplog):
    monkeypatch.setenv("HMEM_SEMANTIC", "embeddings")
    from modules.memory.task.task_context_manager import TaskContextManager
    from modules.memory.task.lexical_retriever import LexicalRetriever
    from core.container import DependencyContainer

    class _FakeContainer:
        def has_service(self, name):
            return name == "embedding_model"

        def get_service(self, name):
            return object()

    monkeypatch.setattr(DependencyContainer, "get_instance", classmethod(lambda cls: _FakeContainer()))
    tcm = TaskContextManager.__new__(TaskContextManager)
    tcm.semantic_enabled = True
    tcm.semantic_min_similarity = 0.65
    with caplog.at_level(logging.WARNING):
        retriever = tcm._get_semantic_retriever()
    assert isinstance(retriever, LexicalRetriever)
    assert "numpy" in caplog.text and "polyrob[memory-vector]" in caplog.text


def test_importance_score_without_numpy_and_without_embeddings(no_numpy):
    """calculate_importance imported numpy unconditionally at function start; a
    bare install scoring findings with no query embedding must not raise."""
    from modules.memory.task.hierarchical_memory import PhaseMemory
    pm = PhaseMemory(phase_name="p", started_step=0)
    pm.add_finding("a finding")
    score = pm.calculate_importance(0)
    assert 0.0 <= score <= 1.0
