"""T1 — Polymarket CLOB client adapter must fail LOUD, never silently.

The legacy `py_clob_client` was archived/unpinned, so a clean install silently
lost all trading. This adapter centralizes the import and turns "trading vanished"
into an explicit, actionable signal.
"""
import pytest

from tools.polymarket import clob_adapter as ad
from tools.polymarket.clob_adapter import PolymarketDependencyError


def test_trade_capability_reports_install_hint_when_missing(monkeypatch):
    monkeypatch.setattr(ad, "CLOB_AVAILABLE", False)
    monkeypatch.setattr(ad, "CLOB_IMPORT_ERROR", "No module named 'py_clob_client_v2'")
    cap = ad.trade_capability()
    assert cap["available"] is False
    assert "py-clob-client-v2" in cap["install_hint"]
    assert cap["reason"]  # non-empty human reason


def test_trade_capability_available_when_importable(monkeypatch):
    monkeypatch.setattr(ad, "CLOB_AVAILABLE", True)
    monkeypatch.setattr(ad, "CLOB_IMPORT_ERROR", None)
    cap = ad.trade_capability()
    assert cap["available"] is True


def test_require_clob_raises_typed_error_with_hint_when_missing(monkeypatch):
    monkeypatch.setattr(ad, "CLOB_AVAILABLE", False)
    monkeypatch.setattr(ad, "CLOB_IMPORT_ERROR", "boom")
    with pytest.raises(PolymarketDependencyError) as exc:
        ad.require_clob()
    assert "py-clob-client-v2" in str(exc.value)


def test_require_clob_noop_when_available(monkeypatch):
    monkeypatch.setattr(ad, "CLOB_AVAILABLE", True)
    # Must not raise.
    ad.require_clob()


def test_dependency_error_is_tool_error_subclass():
    from core.exceptions import ToolError
    assert issubclass(PolymarketDependencyError, ToolError)
