"""098 CL-14: the Hyperliquid SDK probe latches only after a successful import."""
import sys
import types

import pytest

pytest.importorskip("polyrob_markets")
from polyrob_markets.hyperliquid import service as svc


@pytest.fixture
def fresh(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(svc, "_SDK_LOADED", False)
    monkeypatch.setattr(svc, "_SDK_FAILED_AT", 0.0)
    monkeypatch.setattr(svc, "HAS_SDK", False)
    monkeypatch.setattr(svc, "Info", None)
    monkeypatch.setattr(svc, "Exchange", None)
    monkeypatch.setattr(svc.time, "monotonic", lambda: now[0])
    return now


def _install(monkeypatch, ok=True, error=None):
    info = types.ModuleType("hyperliquid.info")
    exch = types.ModuleType("hyperliquid.exchange")
    if ok:
        info.Info, exch.Exchange = object, object
    pkg = types.ModuleType("hyperliquid")
    monkeypatch.setitem(sys.modules, "hyperliquid", pkg)
    monkeypatch.setitem(sys.modules, "hyperliquid.info", info if ok else None)
    monkeypatch.setitem(sys.modules, "hyperliquid.exchange", exch if ok else None)


def test_import_error_retries_after_reset(fresh, monkeypatch):
    _install(monkeypatch, ok=False)  # sys.modules[name] = None -> ImportError
    assert svc._has_sdk() is False and svc._SDK_LOADED is False
    _install(monkeypatch, ok=True)   # the extra is installed while the agent runs
    assert svc._has_sdk() is False   # inside the 60 s retry window
    fresh[0] += 60
    assert svc._has_sdk() is True and svc._SDK_LOADED is True and svc.Info is object


def test_other_error_is_reported_not_cached_silently(fresh, monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "get_component_logger",
                        lambda name: types.SimpleNamespace(warning=seen.append))

    class _Boom(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("broken eth_account")
    monkeypatch.setitem(sys.modules, "hyperliquid", types.ModuleType("hyperliquid"))
    monkeypatch.setitem(sys.modules, "hyperliquid.info", _Boom("hyperliquid.info"))
    assert svc._has_sdk() is False
    assert seen and "RuntimeError" in seen[0] and svc._SDK_LOADED is False
