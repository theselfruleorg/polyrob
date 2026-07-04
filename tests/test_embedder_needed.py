"""Tests for the shared `embedder_needed()` predicate (SSOT for CLI + server).

The sentence-transformers embedder (torch, heavy) must only be built when a deployment
actually needs vectors — KB enabled, MEMORY_BACKEND=local_vector, or local mode. On the
default MEMORY_BACKEND=sqlite (FTS5 keyword recall) it must NOT be built. Both the CLI
(maybe_register_cli_embedder) and the server (initialize_modules) consult this one predicate.
"""

import importlib

import pytest


def _embedder_needed():
    # Reimport to pick up monkeypatched env each time.
    constants = importlib.import_module("agents.task.constants")
    return constants.embedder_needed()


def test_sqlite_backend_does_not_need_embedder(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("KB_ENABLED", "false")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    assert _embedder_needed() is False


def test_local_vector_backend_needs_embedder(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "local_vector")
    monkeypatch.setenv("KB_ENABLED", "false")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    assert _embedder_needed() is True


def test_kb_enabled_needs_embedder(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("KB_ENABLED", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    assert _embedder_needed() is True
