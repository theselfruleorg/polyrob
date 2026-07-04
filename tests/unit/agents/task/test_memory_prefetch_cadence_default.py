"""Phase 1.3 — memory_prefetch_cadence() access-time resolver.

The cadence default must be local-mode-aware (3 under POLYROB_LOCAL, 0 on the
multi-tenant server) and resolved at ACCESS time, not import time — because
POLYROB_LOCAL is set via os.environ.setdefault in bootstrap, which can run after
agents.task.constants is first imported. An explicit MEMORY_PREFETCH_CADENCE always
wins over the local-mode default.
"""
import pytest

from agents.task import constants


def test_cadence_defaults_to_zero_on_server(monkeypatch):
    monkeypatch.delenv("MEMORY_PREFETCH_CADENCE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert constants.memory_prefetch_cadence() == 0


def test_cadence_defaults_to_three_under_local(monkeypatch):
    monkeypatch.delenv("MEMORY_PREFETCH_CADENCE", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert constants.memory_prefetch_cadence() == 3


def test_explicit_cadence_wins_over_local_default(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("MEMORY_PREFETCH_CADENCE", "5")
    assert constants.memory_prefetch_cadence() == 5


def test_explicit_zero_wins_under_local(monkeypatch):
    """An operator can pin first-step-only even in local mode."""
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("MEMORY_PREFETCH_CADENCE", "0")
    assert constants.memory_prefetch_cadence() == 0


def test_bad_value_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("MEMORY_PREFETCH_CADENCE", "notanint")
    assert constants.memory_prefetch_cadence() == 0
