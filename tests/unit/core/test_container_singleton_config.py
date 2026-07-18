"""Regression (P1 finalization): DependencyContainer.get_instance(config) silently
discarded a passed config when a singleton already existed. First-config-wins is
intentional (reconfiguring a live container mid-run is unsafe), but the discard must
be VISIBLE (warned), not silent — behavior is unchanged (existing instance returned).
"""
import logging

from core.container import DependencyContainer


class _FakeConfig:
    is_initialized = True

    def __init__(self, tag):
        self.tag = tag


def _reset():
    DependencyContainer._instance = None


def test_second_config_is_ignored_but_warned(caplog):
    _reset()
    try:
        c1 = DependencyContainer.get_instance(_FakeConfig("first"))
        with caplog.at_level(logging.WARNING):
            c2 = DependencyContainer.get_instance(_FakeConfig("second"))
        assert c1 is c2, "first-config-wins: same singleton returned"
        assert any("IGNORED" in r.message or "ignored" in r.message.lower()
                   for r in caplog.records), "the discarded config must be warned, not silent"
    finally:
        _reset()


def test_same_config_or_none_does_not_warn(caplog):
    _reset()
    try:
        cfg = _FakeConfig("only")
        DependencyContainer.get_instance(cfg)
        with caplog.at_level(logging.WARNING):
            DependencyContainer.get_instance()          # None → no warn
            DependencyContainer.get_instance(cfg)       # same object → no warn
        assert not any("ignored" in r.message.lower() for r in caplog.records)
    finally:
        _reset()
