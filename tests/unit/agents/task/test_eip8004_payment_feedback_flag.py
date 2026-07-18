"""Unit tests for the ``EIP8004_PAYMENT_FEEDBACK`` flag helper (Task 15,
Phase 4). It rides ``EIP8004_ENABLED`` — both must be explicitly on.
"""
from agents.task.constants import eip8004_payment_feedback_enabled


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("EIP8004_ENABLED", raising=False)
    monkeypatch.delenv("EIP8004_PAYMENT_FEEDBACK", raising=False)
    assert eip8004_payment_feedback_enabled() is False


def test_off_when_only_payment_feedback_set(monkeypatch):
    """Rides EIP8004_ENABLED — setting ONLY EIP8004_PAYMENT_FEEDBACK must stay inert."""
    monkeypatch.delenv("EIP8004_ENABLED", raising=False)
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    assert eip8004_payment_feedback_enabled() is False


def test_off_when_only_eip8004_enabled_set(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.delenv("EIP8004_PAYMENT_FEEDBACK", raising=False)
    assert eip8004_payment_feedback_enabled() is False


def test_on_when_both_set(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    assert eip8004_payment_feedback_enabled() is True


def test_explicit_off_beats_eip8004_enabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "false")
    assert eip8004_payment_feedback_enabled() is False
