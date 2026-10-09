"""A money provider replaced by another module is logged and recorded, never silent."""
import logging

from core.money import hooks


def test_a_cross_module_replacement_is_recorded(caplog, monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", dict(hooks._PROVIDERS))
    monkeypatch.setattr(hooks, "_REPLACEMENTS", [])
    first = lambda: None
    first.__module__ = "core.wallet"
    second = lambda: None
    second.__module__ = "some_pack.evil"
    hooks.register_spend_ledger(first)
    hooks.register_spend_ledger(first)          # the same provider again: no record
    assert hooks.replacements() == []
    with caplog.at_level(logging.WARNING, logger="core.money.hooks"):
        hooks.register_spend_ledger(second)
    assert hooks.replacements() == [("spend_ledger", "core.wallet", "some_pack.evil")]
    assert "money hook spend_ledger replaced" in caplog.text
