"""TDD for lazy CLI embedder gate (Task 2).

Tests that maybe_register_cli_embedder():
  - Does NOT register the embedding_model service when KB is off + default backend + not local mode
  - DOES register it when KB_ENABLED=1 (even without local mode)
  - Fails open (no exception) when SentenceTransformer import raises
"""

import sys
import logging
import importlib
import types
import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

class _FakeContainer:
    """Minimal DependencyContainer stand-in."""

    def __init__(self):
        self._services: dict = {}

    def has_service(self, name: str) -> bool:
        return name in self._services

    def register_service(self, name: str, obj) -> None:
        self._services[name] = obj


class _FakeConfig:
    """Minimal BotConfig stand-in."""

    def get_embedding_config(self):
        return {"model_name": "all-MiniLM-L6-v2"}


# ---------------------------------------------------------------------------
# Test: default env — embedder NOT registered, ST never imported
# ---------------------------------------------------------------------------

def test_embedder_not_registered_by_default(monkeypatch):
    """KB off + default MEMORY_BACKEND (sqlite) + not local mode → no embedding_model."""

    # Ensure the gate flags are all off
    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    # Poison sentence_transformers so any accidental import/instantiation fails loudly
    poisoned = types.ModuleType("sentence_transformers")
    poisoned.SentenceTransformer = None  # would raise TypeError if called

    # Reload bootstrap with the poison in place
    monkeypatch.setitem(sys.modules, "sentence_transformers", poisoned)

    # Force fresh import of bootstrap so it sees the patched env
    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)

    container = _FakeContainer()
    config = _FakeConfig()

    bootstrap.maybe_register_cli_embedder(container, config)

    assert not container.has_service("embedding_model"), (
        "embedding_model must NOT be registered when KB is off, MEMORY_BACKEND=sqlite, POLYROB_LOCAL unset"
    )


# ---------------------------------------------------------------------------
# Test: KB_ENABLED=1 — embedder IS registered via the stub
# ---------------------------------------------------------------------------

class _StubST:
    """Stand-in for SentenceTransformer that records instantiation."""
    _instances: list = []

    def __init__(self, model_name: str):
        self.model_name = model_name
        _StubST._instances.append(self)


def test_embedder_registered_lazily_when_kb_enabled(monkeypatch):
    """KB_ENABLED=1 → a LAZY embedding_model is registered but NOT built eagerly.

    The heavy SentenceTransformer (torch + HF Hub network validation) must not be
    constructed at registration — it builds on first actual vector use.
    """
    monkeypatch.setenv("KB_ENABLED", "1")
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)
    from core.embedding import LazyEmbedder

    container = _FakeContainer()
    config = _FakeConfig()

    bootstrap.maybe_register_cli_embedder(container, config)

    assert container.has_service("embedding_model"), (
        "embedding_model must be registered when KB_ENABLED=1"
    )
    svc = container._services["embedding_model"]
    assert isinstance(svc, LazyEmbedder)
    assert svc.loaded is False, "the model must NOT be built at registration (lazy)"


# ---------------------------------------------------------------------------
# Test: import error → fail-open, no exception propagates
# ---------------------------------------------------------------------------

def test_embedder_failopen_when_st_not_installed(monkeypatch):
    """sentence_transformers not installed → fail-open to FTS-only, service absent, no raise.

    Availability is probed with a torch-free importlib.util.find_spec; when it returns None
    the embedder is not registered so consumers degrade gracefully to keyword (FTS) recall.
    """
    monkeypatch.setenv("KB_ENABLED", "1")
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    import importlib.util as _ilu
    _real_find_spec = _ilu.find_spec

    def _fake_find_spec(name, *a, **k):
        if name == "sentence_transformers":
            return None
        return _real_find_spec(name, *a, **k)

    monkeypatch.setattr(_ilu, "find_spec", _fake_find_spec)

    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)

    container = _FakeContainer()
    config = _FakeConfig()

    # Must not raise
    bootstrap.maybe_register_cli_embedder(container, config)

    assert not container.has_service("embedding_model"), (
        "embedding_model must NOT be registered when sentence_transformers is unavailable"
    )


# ---------------------------------------------------------------------------
# Test: MEMORY_BACKEND=local_vector → embedder IS registered
# ---------------------------------------------------------------------------

def test_embedder_registered_when_local_vector_backend(monkeypatch):
    """MEMORY_BACKEND=local_vector → embedding_model service is registered."""

    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.setenv("MEMORY_BACKEND", "local_vector")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    fake_st_mod = types.ModuleType("sentence_transformers")
    _StubST._instances = []
    fake_st_mod.SentenceTransformer = _StubST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_mod)

    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)

    container = _FakeContainer()
    config = _FakeConfig()

    bootstrap.maybe_register_cli_embedder(container, config)

    assert container.has_service("embedding_model"), (
        "embedding_model must be registered when MEMORY_BACKEND=local_vector"
    )


# ---------------------------------------------------------------------------
# Test: POLYROB_LOCAL=1 → embedder IS registered
# ---------------------------------------------------------------------------

def test_embedder_registered_in_local_mode(monkeypatch):
    """POLYROB_LOCAL=1 → embedding_model service is registered (local mode gate)."""

    monkeypatch.delenv("KB_ENABLED", raising=False)
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")

    fake_st_mod = types.ModuleType("sentence_transformers")
    _StubST._instances = []
    fake_st_mod.SentenceTransformer = _StubST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_mod)

    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)

    container = _FakeContainer()
    config = _FakeConfig()

    bootstrap.maybe_register_cli_embedder(container, config)

    assert container.has_service("embedding_model"), (
        "embedding_model must be registered when POLYROB_LOCAL=1"
    )


# ---------------------------------------------------------------------------
# Test: already-registered → helper is a no-op (idempotent)
# ---------------------------------------------------------------------------

def test_embedder_not_double_registered(monkeypatch):
    """If embedding_model already in the container, helper must not re-register."""

    monkeypatch.setenv("KB_ENABLED", "1")
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    call_count = {"n": 0}

    class _CountedST:
        def __init__(self, model_name: str):
            call_count["n"] += 1

    fake_st_mod = types.ModuleType("sentence_transformers")
    fake_st_mod.SentenceTransformer = _CountedST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_mod)

    import core.bootstrap as bootstrap
    importlib.reload(bootstrap)

    container = _FakeContainer()
    existing = object()
    container.register_service("embedding_model", existing)

    config = _FakeConfig()
    bootstrap.maybe_register_cli_embedder(container, config)

    # The pre-existing value must be unchanged
    assert container._services["embedding_model"] is existing
    assert call_count["n"] == 0, "SentenceTransformer must not be instantiated if already registered"
