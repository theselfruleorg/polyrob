"""X402_INVOICE_MAX_USD / X402_INVOICE_DAILY_MAX have ONE resolver each.

The console (webview/pages.py) reads the same functions the invoicing rail
enforces, and a non-finite or negative value falls back to the default.
"""

import pytest

from modules.x402.invoicing import invoice_daily_max, invoice_max_usd


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("X402_INVOICE_MAX_USD", raising=False)
    monkeypatch.delenv("X402_INVOICE_DAILY_MAX", raising=False)


def test_defaults():
    assert invoice_max_usd() == 50.0
    assert invoice_daily_max() == 10


@pytest.mark.parametrize("raw", ["", "abc", "nan", "inf", "-inf", "-5"])
def test_max_usd_bad_values_fall_back(monkeypatch, raw):
    monkeypatch.setenv("X402_INVOICE_MAX_USD", raw)
    assert invoice_max_usd() == 50.0


@pytest.mark.parametrize("raw", ["", "abc", "2.5", "-1"])
def test_daily_max_bad_values_fall_back(monkeypatch, raw):
    monkeypatch.setenv("X402_INVOICE_DAILY_MAX", raw)
    assert invoice_daily_max() == 10


def test_explicit_values(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_MAX_USD", "12.5")
    monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "3")
    assert invoice_max_usd() == 12.5
    assert invoice_daily_max() == 3


def test_console_caps_use_the_resolvers(monkeypatch):
    from webview import pages
    monkeypatch.setenv("X402_INVOICE_MAX_USD", "nan")
    monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "4")
    caps = pages._ledger_caps(None)
    assert caps["invoice_max_usd"] == 50.0
    assert caps["invoice_daily_max"] == 4
