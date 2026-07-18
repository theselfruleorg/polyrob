"""Unit tests for the ``INVOICE_CARD_ENABLED`` flag helper (Task 6).

``invoice_card_enabled()`` defaults to ``local_mode_enabled()`` because
``INVOICE_CARD_ENABLED`` is in ``_SAFE_LOCAL_FLAGS`` (via ``_safe_autonomy_default``).
An explicit ``INVOICE_CARD_ENABLED`` env value always wins over the local-mode default.
"""

from agents.task.constants import invoice_card_enabled


def test_off_by_default(monkeypatch):
    for k in ("INVOICE_CARD_ENABLED", "POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    assert invoice_card_enabled() is False


def test_explicit_on(monkeypatch):
    for k in ("POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVOICE_CARD_ENABLED", "true")
    assert invoice_card_enabled() is True


def test_on_under_local(monkeypatch):
    monkeypatch.delenv("INVOICE_CARD_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert invoice_card_enabled() is True


def test_explicit_off_beats_local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("INVOICE_CARD_ENABLED", "false")
    assert invoice_card_enabled() is False
