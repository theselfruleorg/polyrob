"""MEMORY_BACKEND default had three independent copies (backend_factory,
policy.embedder_needed, doctor.resolve_memory_backend) that could silently
diverge (027 rider). One SSOT in core.config_policy.policy now."""

import os


def test_resolved_memory_backend_local_default(monkeypatch):
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    from core.config_policy.policy import resolved_memory_backend

    assert resolved_memory_backend() == "local_vector"


def test_resolved_memory_backend_explicit_wins(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    from core.config_policy.policy import resolved_memory_backend

    assert resolved_memory_backend() == "sqlite"


def test_doctor_view_matches_the_ssot(monkeypatch):
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    from cli.commands.doctor import resolve_memory_backend
    from core.config_policy.policy import resolved_memory_backend

    assert resolve_memory_backend(dict(os.environ), True) == resolved_memory_backend()
